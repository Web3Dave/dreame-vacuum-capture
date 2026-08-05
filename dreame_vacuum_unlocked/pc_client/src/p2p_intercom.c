/* p2p_intercom - open a live video session on the XP2P link, then stream
 * audio to the vacuum speaker over the SAME p2p handle (talk-back/intercom).
 *
 * The hypothesis behind this binary: the robot only routes talk-back audio to
 * its speaker when there is an ACTIVE live-view (monitor/video) session on the
 * p2p link. p2p_speak opened a fresh session with no video and stayed silent;
 * this binary keeps the live URL/session up on the handle it sends voice on.
 *
 * Usage: XP2P_INFO=<p2p_info> ./p2p_intercom <config.txt> <raw_audio.bin> [channel] [crypto]
 */
#define _DEFAULT_SOURCE
#include <stdio.h>
#include <stdlib.h>
#include <unistd.h>
#include <inttypes.h>
#include <string.h>
#include "p2p_api.h"

static char sg_product_id[64] = {0};
static char sg_device_name[64] = {0};
static char sg_app_id[128] = {0};
static char sg_app_key[128] = {0};

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

int main(int argc, char **argv)
{
    if (argc < 3) {
        fprintf(stderr, "Usage: XP2P_INFO=.. %s <config.txt> <raw_audio.bin> [channel] [crypto]\n", argv[0]);
        return 1;
    }
    int channel = argc > 3 ? atoi(argv[3]) : 0;
    bool crypto  = argc > 4 ? (atoi(argv[4]) != 0) : false;

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

    FILE *fp = fopen(argv[2], "rb");
    if (!fp) { perror("fopen audio"); stopService(sg_device_name); return 1; }

    unsigned char chunk[8192];
    size_t n, total = 0;
    int rc;
    while ((n = fread(chunk, 1, sizeof(chunk), fp)) > 0) {
        rc = QcloudSendVoice(handle, channel, crypto, chunk, n);
        if (rc != 0) {
            fprintf(stderr, "dataSend rc=%d at %zu bytes\n", rc, total);
            break;
        }
        total += n;
        fprintf(stderr, "sent %zu bytes (total %zu)\n", n, total);
        fflush(stderr);
        __SYS_SLEEP_MS(200);
    }
    fclose(fp);
    fprintf(stderr, "done, total %zu bytes; closing send service\n", total);

    /* data==NULL, len==0 closes the send service */
    QcloudSendVoice(handle, channel, crypto, NULL, 0);
    __SYS_SLEEP_MS(300);
    stopService(sg_device_name);
    return 0;
}
