/* p2p_intercom - open a live video session on the XP2P link, then stream
 * audio to the vacuum speaker over the SAME p2p handle (talk-back/intercom).
 *
 * The hypothesis behind this binary: the robot only routes talk-back audio to
 * its speaker when there is an ACTIVE live-view (monitor/video) session on the
 * p2p link. p2p_speak opened a fresh session with no video and stayed silent;
 * this binary keeps the live URL/session up on the handle it sends voice on.
 *
 * Audio framing: the device speaker expects a real FLV container (the same
 * one the app's own AudioRecordUtil/PCMEncoder/FLVPacker pipeline produces -
 * AAC-LC, 16kHz, mono), delivered ONE FLV TAG PER SEND CALL, not arbitrary
 * byte chunks - the app's XP2P.dataSend() is called once per muxed FLV tag,
 * and the device's stream parser appears to require that alignment. Callers
 * must hand this binary an already-muxed .flv file (see
 * dreame_lib/flv_audio.py's build_send_file()) - this binary just walks the
 * FLV container tag-by-tag and pushes each tag as one send call, paced to
 * real time between raw audio frames.
 *
 * Two modes:
 *   One-shot:  XP2P_INFO=<p2p_info> ./p2p_intercom <config.txt> <file.flv> [cmd] [crypto]
 *              Sends exactly one pre-muxed FLV file, then closes and exits -
 *              used by a single request/response call (e.g. /speak).
 *   Streaming: XP2P_INFO=<p2p_info> ./p2p_intercom <config.txt> - [cmd] [crypto]
 *              Opens the p2p handle + live session once, then reads
 *              newline-delimited paths to pre-muxed .flv files from stdin,
 *              sending each in turn (printing "SENT <path> rc=<n>" to stdout
 *              after each one completes) until it reads a line "STOP" or
 *              hits EOF, then closes the send service and exits - used to
 *              keep one open channel alive across multiple clips
 *              (/speak/start, /speak/send x N, /speak/stop).
 */
#define _DEFAULT_SOURCE
#include <stdio.h>
#include <stdlib.h>
#include <unistd.h>
#include <inttypes.h>
#include <string.h>
#include <stdbool.h>
#include "p2p_api.h"

static char sg_product_id[64] = {0};
static char sg_device_name[64] = {0};
static char sg_app_id[128] = {0};
static char sg_app_key[128] = {0};

#define FLV_TYPE_AUDIO      8
#define FLV_TYPE_VIDEO      9
#define FLV_TYPE_SCRIPT     18
#define N_TAG_SIZE          4  // previous tag size
#define FLV_HEADER_SIZE     9  // DataOffset included
#define FLV_TAG_HEADER_SIZE 11 // StreamID included

struct flv_header_t {
    uint8_t FLV[3];
    uint8_t version;
    uint8_t audio;
    uint8_t video;
    uint32_t offset; // data offset
};

struct flv_tag_header_t {
    uint8_t filter; // 0-No pre-processing required
    uint8_t type;   // 8-audio, 9-video, 18-script data
    uint32_t size;  // data size
    uint32_t timestamp;
    uint32_t streamId;
};

static inline uint32_t be_read_uint32(const uint8_t *ptr)
{
    return (ptr[0] << 24) | (ptr[1] << 16) | (ptr[2] << 8) | ptr[3];
}

static int flv_header_read(struct flv_header_t *flv, const uint8_t *buf, size_t len)
{
    if (len < FLV_HEADER_SIZE || 'F' != buf[0] || 'L' != buf[1] || 'V' != buf[2]) {
        return -1;
    }
    flv->FLV[0] = buf[0]; flv->FLV[1] = buf[1]; flv->FLV[2] = buf[2];
    flv->version = buf[3];
    flv->audio = (buf[4] >> 2) & 0x01;
    flv->video = buf[4] & 0x01;
    flv->offset = be_read_uint32(buf + 5);
    return FLV_HEADER_SIZE;
}

static int flv_tag_header_read(struct flv_tag_header_t *tag, const uint8_t *buf, size_t len)
{
    if (len < FLV_TAG_HEADER_SIZE) {
        return -1;
    }
    tag->type = buf[0] & 0x1F;
    tag->filter = (buf[0] >> 5) & 0x01;
    if (FLV_TYPE_VIDEO != tag->type && FLV_TYPE_AUDIO != tag->type && FLV_TYPE_SCRIPT != tag->type)
        return -1;
    tag->size = (buf[1] << 16) | (buf[2] << 8) | buf[3];
    tag->timestamp = (buf[4] << 16) | (buf[5] << 8) | buf[6] | (buf[7] << 24);
    tag->streamId = (buf[8] << 16) | (buf[9] << 8) | buf[10];
    return FLV_TAG_HEADER_SIZE;
}

static const char *msg_handle_cb(const char *id, XP2PType type, const char *msg)
{
    if (type == XP2PTypeLog) {
        fprintf(stderr, "[%s] %s", id, msg);
    } else if (type == XP2PTypeDetectReady) {
        fprintf(stderr, "[id=%s] p2p ready\n", id);
    } else if (type == XP2PTypeDetectError) {
        fprintf(stderr, "[id=%s] p2p detect error\n", id);
    } else if (type == XP2PTypeDisconnect) {
        fprintf(stderr, "[id=%s] p2p disconnected\n", id);
    } else if (type == XP2PTypeStreamEnd) {
        fprintf(stderr, "[id=%s] stream end\n", id);
    }
    return "";
}

static void av_recv_handle_cb(const char *id, uint8_t *buf, size_t len)
{
    (void)id; (void)buf; (void)len;
}

static char *device_data_recv_handle_cb(const char *id, uint8_t *buf, size_t len)
{
    (void)id; (void)buf; (void)len;
    return "";
}

static void parse_config(const char *file)
{
    FILE *fp = fopen(file, "r");
    if (!fp) { perror("fopen"); exit(1); }
    char line[256];
    while (fgets(line, sizeof(line), fp)) {
        char *nl = strchr(line, '\n'); if (nl) *nl = 0;
        char *p;
        if ((p = strstr(line, "product_id="))) strncpy(sg_product_id, p + 11, sizeof(sg_product_id)-1);
        else if ((p = strstr(line, "device_name="))) strncpy(sg_device_name, p + 12, sizeof(sg_device_name)-1);
        else if ((p = strstr(line, "app_id="))) strncpy(sg_app_id, p + 7, sizeof(sg_app_id)-1);
        else if ((p = strstr(line, "app_key="))) strncpy(sg_app_key, p + 8, sizeof(sg_app_key)-1);
    }
    fclose(fp);
    fprintf(stderr, "product_id=%s device_name=%s\n", sg_product_id, sg_device_name);
}

/* Walk a pre-muxed FLV file tag-by-tag, sending each chunk (file header,
 * audio sequence header, then one raw-AAC tag per frame) as its own
 * QcloudSendVoiceCommand() call, exactly mirroring the app's
 * onFLV()->XP2P.dataSend() call boundaries. Paces ~64ms between raw audio
 * frames to match real-time playback. Returns 0 on success, -1 on error. */
static int send_flv_file(void *handle, const char *path, const char *cmd, bool crypto)
{
    FILE *fp = fopen(path, "rb");
    if (!fp) { perror("send_flv_file: fopen"); return -1; }
    fseek(fp, 0, SEEK_END);
    long fsize = ftell(fp);
    fseek(fp, 0, SEEK_SET);
    if (fsize <= 0) { fclose(fp); return -1; }

    uint8_t *buf = (uint8_t *)malloc((size_t)fsize);
    if (!buf) { fclose(fp); return -1; }
    if (fread(buf, 1, (size_t)fsize, fp) != (size_t)fsize) {
        perror("send_flv_file: fread");
        free(buf); fclose(fp);
        return -1;
    }
    fclose(fp);

    struct flv_header_t flv_hdr;
    int hdr_len = flv_header_read(&flv_hdr, buf, (size_t)fsize);
    if (hdr_len < 0) {
        fprintf(stderr, "send_flv_file: %s is not a valid FLV stream\n", path);
        free(buf);
        return -1;
    }

    size_t offset = (size_t)hdr_len + N_TAG_SIZE; /* header + PreviousTagSize0 */
    int rc = QcloudSendVoiceCommand(handle, cmd, crypto, buf, offset);
    if (rc != 0) {
        fprintf(stderr, "send_flv_file: send(header) failed rc=%d\n", rc);
        free(buf);
        return -1;
    }

    int n_tags = 0;
    while (offset + FLV_TAG_HEADER_SIZE <= (size_t)fsize) {
        struct flv_tag_header_t tag;
        int tag_hdr_len = flv_tag_header_read(&tag, buf + offset, (size_t)fsize - offset);
        if (tag_hdr_len < 0) {
            fprintf(stderr, "send_flv_file: bad tag header at offset %zu\n", offset);
            break;
        }
        size_t chunk_len = (size_t)tag_hdr_len + tag.size + N_TAG_SIZE;
        if (offset + chunk_len > (size_t)fsize) {
            fprintf(stderr, "send_flv_file: truncated tag at offset %zu\n", offset);
            break;
        }

        rc = QcloudSendVoiceCommand(handle, cmd, crypto, buf + offset, chunk_len);
        if (rc != 0) {
            fprintf(stderr, "send_flv_file: send(tag #%d) failed rc=%d\n", n_tags, rc);
            break;
        }
        n_tags++;

        /* Only pace between raw audio frames (AACPacketType=1) - the sequence
         * header should go out immediately alongside the file header so the
         * device has the AudioSpecificConfig before the first frame arrives. */
        bool is_raw_audio_frame = (tag.type == FLV_TYPE_AUDIO) && (tag.size > 0) &&
                                   (buf[offset + tag_hdr_len + 1] == 0x01);
        offset += chunk_len;
        if (is_raw_audio_frame) {
            __SYS_SLEEP_MS(64);
        }
    }

    fprintf(stderr, "send_flv_file: %s -> sent %d tags\n", path, n_tags);
    free(buf);
    return rc;
}

int main(int argc, char **argv)
{
    if (argc < 3) {
        fprintf(stderr, "Usage: XP2P_INFO=.. %s <config.txt> <file.flv|-> [cmd] [crypto]\n", argv[0]);
        return 1;
    }
    const char *file_arg = argv[2];
    const char *cmd      = argc > 3 ? argv[3] : "channel=0";
    bool crypto          = argc > 4 ? (atoi(argv[4]) != 0) : false;

    parse_config(argv[1]);

    QcloudP2PRegister(av_recv_handle_cb, msg_handle_cb, device_data_recv_handle_cb,
                      sg_app_id, sg_app_key);

    const char *xp2p_info = getenv("XP2P_INFO");
    fprintf(stderr, "XP2P_INFO=%s\n", xp2p_info ? "(set)" : "(none)");
    void *handle = QcloudP2PWanInt(sg_device_name, sg_product_id, sg_device_name, xp2p_info);
    if (!handle) {
        fprintf(stderr, "failed to init p2p\n");
        return 1;
    }
    fprintf(stderr, "p2p init success\n");

    /* Device capability probe (live vs voice) on this handle. */
    {
        const char *st = "action=inner_define&channel=0&cmd=get_device_st&type=live&quality=standard";
        unsigned char buf[1024] = {0};
        int len = QcloudPostCommandRequest(handle, (const unsigned char *)st, strlen(st), buf, 3*1000*1000);
        fprintf(stderr, "live get_device_st: len=%d body=%.*s\n", len, len>0?len:0, buf);
    }
    {
        const char *st = "action=inner_define&channel=0&cmd=get_device_st&type=voice&quality=standard";
        unsigned char buf[1024] = {0};
        int len = QcloudPostCommandRequest(handle, (const unsigned char *)st, strlen(st), buf, 3*1000*1000);
        fprintf(stderr, "voice get_device_st: len=%d body=%.*s\n", len, len>0?len:0, buf);
    }

    /* KEY: start the live video session on THIS handle so the device has an
     * active live-view session to route talk-back audio into. */
    char *url = QcloudRequestLiveUrl(handle, STREAM_TYPE_HIGH, 0, false);
    fprintf(stderr, "LIVE_URL: %s\n", url ? url : "(null)");
    fflush(stderr);

    /* Small settle so the live session is established before voice. */
    __SYS_SLEEP_MS(1500);

    int overall_rc = 0;
    if (strcmp(file_arg, "-") == 0) {
        /* Streaming mode: read file paths from stdin, one per line, until
         * "STOP" or EOF, sending each and reporting completion so the caller
         * (app.py) can synchronize before issuing /speak/stop. */
        char line[1024];
        while (fgets(line, sizeof(line), stdin)) {
            char *nl = strchr(line, '\n'); if (nl) *nl = 0;
            if (line[0] == 0) continue;
            if (strcmp(line, "STOP") == 0) break;
            int rc = send_flv_file(handle, line, cmd, crypto);
            printf("SENT %s rc=%d\n", line, rc);
            fflush(stdout);
            if (rc != 0) overall_rc = rc;
        }
    } else {
        /* One-shot mode: send exactly one pre-muxed file. */
        overall_rc = send_flv_file(handle, file_arg, cmd, crypto);
    }

    fprintf(stderr, "closing send service\n");
    /* data==NULL, len==0 closes the send service */
    QcloudSendVoiceCommand(handle, cmd, crypto, NULL, 0);
    __SYS_SLEEP_MS(300);
    stopService(sg_device_name);
    return overall_rc == 0 ? 0 : 1;
}
