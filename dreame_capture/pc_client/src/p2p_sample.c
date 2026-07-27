#ifdef WINDOWS
#include <Windows.h>
#include <winsock.h>
#endif
#include <stdio.h>
#include <stdlib.h>
#include <inttypes.h>
#include <string.h>
#include "p2p_api.h"

#include <time.h>
#include <stdint.h> // portable: uint64_t   MSVC: __int64 

#define P2P_LIVE_STRING     "ipc.flv?action=live"

static uint8_t sg_data[] = {0x00, 0x01, 0x02, 0x03, 0x04, 0x05, 0x06, 0x07, 0x08, 0x09, 0x0a, 0x0b, 0x0c, 0x0d, 0x0e, 0x0f};

static char sg_product_id[10 + 1] = {0};
static char sg_device_name[64 + 1] = {0};
static char sg_app_id[128] = {0};
static char sg_app_key[128] = {0};
static char sg_lan_host[16] = {0};
static char sg_lan_port[8] = {0};

#define FLV_TYPE_AUDIO		8
#define FLV_TYPE_VIDEO		9
#define FLV_TYPE_SCRIPT		18
#define N_TAG_SIZE			4	// previous tag size
#define FLV_HEADER_SIZE		9	// DataOffset included
#define FLV_TAG_HEADER_SIZE	11	// StreamID included

struct flv_header_t
{
	uint8_t FLV[3];
	uint8_t version;
	uint8_t audio;
	uint8_t video;
	uint32_t offset; // data offset
};

struct flv_tag_header_t
{
	uint8_t filter; // 0-No pre-processing required
	uint8_t type; // 8-audio, 9-video, 18-script data
	uint32_t size; // data size
	uint32_t timestamp;
	uint32_t streamId;
};

static inline uint32_t be_read_uint32(const uint8_t* ptr)
{
    return (ptr[0] << 24) | (ptr[1] << 16) | (ptr[2] << 8) | ptr[3];
}

int flv_header_read(struct flv_header_t* flv, const uint8_t* buf, size_t len)
{
    if (len < FLV_HEADER_SIZE || 'F' != buf[0] || 'L' != buf[1] || 'V' != buf[2]) {
        return -1;
    }

    flv->FLV[0] = buf[0];
    flv->FLV[1] = buf[1];
    flv->FLV[2] = buf[2];
    flv->version = buf[3];

    if (!(0x00 == (buf[4] & 0xF8) && 0x00 == (buf[4] & 0x20))) {
        return -1;
    }
    flv->audio = (buf[4] >> 2) & 0x01;
    flv->video = buf[4] & 0x01;
    flv->offset = be_read_uint32(buf + 5);

    return FLV_HEADER_SIZE;
}

int flv_tag_header_read(struct flv_tag_header_t* tag, const uint8_t* buf, size_t len)
{
    if (len < FLV_TAG_HEADER_SIZE) {
        return -1;
    }

    // TagType
    tag->type = buf[0] & 0x1F;
    tag->filter = (buf[0] >> 5) & 0x01;
    if (FLV_TYPE_VIDEO != tag->type && FLV_TYPE_AUDIO != tag->type && FLV_TYPE_SCRIPT != tag->type)
        return -1;

    // DataSize
    tag->size = (buf[1] << 16) | (buf[2] << 8) | buf[3];

    // TimestampExtended | Timestamp
    tag->timestamp = (buf[4] << 16) | (buf[5] << 8) | buf[6] | (buf[7] << 24);

    // StreamID Always 0
    tag->streamId = (buf[8] << 16) | (buf[9] << 8) | buf[10];

    return FLV_TAG_HEADER_SIZE;
}

#ifdef WINDOWS
char *strsep(char **stringp, const char *delim)
{
    char *s;
    const char *spanp;
    int c, sc;
    char *tok;
    if ((s = *stringp)== NULL)
        return (NULL);
    for (tok = s;;) {
        c = *s++;
        spanp = delim;
        do {
            if ((sc =*spanp++) == c) {
                if (c == 0)
                    s = NULL;
                else
                    s[-1] = 0;
                *stringp = s;
                return (tok);
            }
        } while (sc != 0);
    }
    /* NOTREACHED */
}
#endif

const char *msg_handle_cb(const char *id, XP2PType type, const char *msg)
{
    if (type == XP2PTypeClose) {  //av recv close callback
    } else if (type == XP2PTypeCmd) {  //command request callback
        printf("[%s]: command response: %s\n", id, msg);
    } else if (type == XP2PTypeSaveFileOn) {//是否将音视频流保存成文件
        return "1";
    } else if (type == XP2PTypeSaveFileUrl) { //文件存储路径
        return "raw_video.data";
    } else if (type == XP2PTypeLog) {
        //save or print(do not used LOG*) log message
        printf("[%s]:%s", id, msg);
    } else if (type == XP2PTypeDisconnect || type == XP2PTypeDetectError || type == XP2PTypeDetectReady || type == XP2PTypeStreamEnd || type == XP2PTypeCmdNOReturn) { //p2p链路事件

    }
    return "";
}

void av_recv_handle_cb(const char *id, uint8_t *recv_buf, size_t recv_len)
{
    //printf("av_recv_handle_cb id: %s, len: %d\n", id, recv_len);
    return;
}

char *device_data_recv_handle_cb(const char *id, uint8_t *recv_buf, size_t recv_len)
{
    //printf("id: %s, len: %d\n", id, recv_len);
    return "";
}

static int _parse_config_file(char *file)
{
    FILE *fp;
    char strLine[128] = {0};
    char *p = NULL;

    printf("parse config file: %s\n", file);
    fp = fopen(file, "r");
    if (fp == NULL) {
        perror("fopen");
        return -1;
    }

    while (!feof(fp)){
        memset(strLine, 0, sizeof(strLine));
        fgets(strLine, sizeof(strLine), fp);

        if (strLine[strlen(strLine) - 2] == '\r')
            strLine[strlen(strLine) - 2] = '\0';
        else if (strLine[strlen(strLine) - 1] == '\n')
            strLine[strlen(strLine) - 1] = '\0';

        p = strstr(strLine, "app_id");
        if (NULL != p){
            memset(sg_app_id, 0, sizeof(sg_app_id));
            strcpy(sg_app_id, p + sizeof("app_id=") - 1);
            printf("parse app id: %s\n", sg_app_id);
        }

        p = strstr(strLine, "app_key");
        if (NULL != p){
            memset(sg_app_key, 0, sizeof(sg_app_key));
            strcpy(sg_app_key, p + sizeof("app_key=") - 1);
            printf("parse app key: %s\n", sg_app_key);
        }

        p = strstr(strLine, "product_id");
        if (NULL != p){
            memset(sg_product_id, 0, sizeof(sg_product_id));
            strcpy(sg_product_id, p + sizeof("product_id=") - 1);
            printf("parse product id: %s\n", sg_product_id);
        }

        p = strstr(strLine, "device_name");
        if (NULL != p){
            memset(sg_device_name, 0, sizeof(sg_device_name));
            strcpy(sg_device_name, p + sizeof("device_name=") - 1);
            printf("parse device name: %s\n", sg_device_name);
        }

        p = strstr(strLine, "lan_host");
        if (NULL != p){
            memset(sg_lan_host, 0, sizeof(sg_lan_host));
            strcpy(sg_lan_host, p + sizeof("lan_host=") - 1);
            printf("parse lan host: %s\n", sg_lan_host);
        }

        p = strstr(strLine, "lan_port");
        if (NULL != p){
            memset(sg_lan_port, 0, sizeof(sg_lan_port));
            strcpy(sg_lan_port, p + sizeof("lan_port=") - 1);
            printf("parse lan port: %s\n", sg_lan_port);
        }
    }
    fclose(fp);

    return 0;
}

int main(int argc, char** argv)
{
    void *handle = NULL;

    if (argc < 2){
        printf("Usage: ./p2p_sample.exe <config file>\n");
        return;
    }
    _parse_config_file(argv[1]);

    QcloudP2PRegister(av_recv_handle_cb, msg_handle_cb, device_data_recv_handle_cb, sg_app_id, sg_app_key);

    if (argc > 2) {
        handle = QcloudP2PLanInt(sg_device_name, sg_product_id, sg_device_name, sg_lan_host, sg_lan_port);
    } else {
        const char *xp2p_info = getenv("XP2P_INFO");
        printf("using XP2P_INFO from env: %s\n", xp2p_info ? xp2p_info : "(none, will fetch via cloud API)");
        handle = QcloudP2PWanInt(sg_device_name, sg_product_id, sg_device_name, xp2p_info);
    }

    if (!handle) {
        printf("failed to init app p2p\n");
        return NULL;
    }

    printf("app p2p init success for %s %s\n", sg_product_id, sg_device_name);

    /* Query device state before requesting the stream - distinguishes
     * "live" (video) vs "voice" (audio-only) capability per the SDK docs. */
    {
        const char *st_cmd = "action=inner_define&channel=0&cmd=get_device_st&type=live&quality=standard";
        unsigned char recv_buf[1024] = {0};
        int recv_len = QcloudPostCommandRequest(handle, (const unsigned char *)st_cmd, strlen(st_cmd), recv_buf, 3*1000*1000);
        printf("GET_DEVICE_ST(live): recv_len=%d body=%.*s\n", recv_len, recv_len > 0 ? recv_len : 0, recv_buf);
        fflush(stdout);
    }
    {
        const char *st_cmd = "action=inner_define&channel=0&cmd=get_device_st&type=voice&quality=standard";
        unsigned char recv_buf[1024] = {0};
        int recv_len = QcloudPostCommandRequest(handle, (const unsigned char *)st_cmd, strlen(st_cmd), recv_buf, 3*1000*1000);
        printf("GET_DEVICE_ST(voice): recv_len=%d body=%.*s\n", recv_len, recv_len > 0 ? recv_len : 0, recv_buf);
        fflush(stdout);
    }

    /* Print the live URL once in a fixed, greppable format so a wrapper
     * process (e.g. Python reading our stdout) can parse it, then just
     * stay alive so the local FLV proxy this handle owns keeps running.
     * `docker stop`/SIGTERM ends the process. */
    char *url = QcloudRequestLiveUrl(handle, STREAM_TYPE_STANDARD, 0, false);
    printf("LIVE_URL: %s\n", url ? url : "(null)");
    fflush(stdout);

    while (1) {
        __SYS_SLEEP_MS(1000);
    }

    stopService(sg_device_name);

	return 0;
}