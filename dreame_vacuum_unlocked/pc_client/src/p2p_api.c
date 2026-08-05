#ifdef WINDOWS
#include <Windows.h>
#endif
#include "p2p_api.h"
#include <inttypes.h>
#include <stdint.h>  // portable: uint64_t   MSVC: __int64
#include <stdio.h>
#include <stdlib.h>
#include <stdarg.h>
#include <string.h>
#include <time.h>

#include "appWrapper.h"

typedef struct {
    char id[64];
    char *url;
    char *LiveUrl;
    char *PlaybackUrl;
    char isLan;
    char isReady;
    char isSendService;
} P2pApiHandle;

#define QCLOUD_MALLOC malloc
#define QCLOUD_FREE   free

#define MAX_URL_LENGTH    128
#define MAX_DEVICE_NUMBER 4

#define QCLOUD_WAN_LIVE_URL "%sipc.flv?action=live&channel=%d&quality=%s"
#define QCLOUD_LAN_LIVE_URL \
    "%sipc.flv?action=live&_protocol=tcp&_port=%d&channel=%d&quality=%s&_crypto=off"

#define QCLOUD_LAN_PLYABCAK_URL                                                               \
    "%sipc.flv?action=playback&_protocol=tcp&_port=%d&channel=%d&start_time=%d&end_time=%d&_" \
    "crypto=off"
#define QCLOUD_WAN_PLYABCAK_URL "%sipc.flv?action=playback&channel=%d&start_time=%d&end_time=%d"

static P2pApiHandle *sgP2pApiHandle[MAX_DEVICE_NUMBER] = {NULL};
msg_handle_t msgHandleProcCb;

static int QcloudSnprintf(char *str, const int len, const char *fmt, ...)
{
    va_list args;
    int rc;

    va_start(args, fmt);
    rc = vsnprintf(str, len, fmt, args);
    va_end(args);

    return rc;
}

static int QcloudSetHandle(P2pApiHandle *handle)
{
    int i  = 0;
    int rc = 0;

    for (i = 0; i < MAX_DEVICE_NUMBER; i++) {
        if (!sgP2pApiHandle[i]) {
            sgP2pApiHandle[i] = handle;
        }
    }

    if (i > MAX_DEVICE_NUMBER) {
        printf("exceed max device number!");
        rc = -1;
    }

    return rc;
}

static P2pApiHandle *QcloudGetHandle(const char *id)
{
    int i = 0;

    for (i = 0; i < MAX_DEVICE_NUMBER; i++) {
        if (sgP2pApiHandle[i] &&
            (!strncmp(sgP2pApiHandle[i]->id, id, strlen(sgP2pApiHandle[i]->id)))) {
            break;
        }
    }

    if (i > MAX_DEVICE_NUMBER) {
        printf("don't find %s handle!", id);
        return NULL;
    }

    return sgP2pApiHandle[i];
}

static void QcloudDelHandle(P2pApiHandle *handle)
{
    int i = 0;

    for (i = 0; i < MAX_DEVICE_NUMBER; i++) {
        if (sgP2pApiHandle[i] && (sgP2pApiHandle[i] == handle)) {
            sgP2pApiHandle[i] = NULL;
            break;
        }
    }
}

static const char *QcloudMsgProc(const char *id, XP2PType type, const char *msg)
{
    P2pApiHandle *pHadnle = QcloudGetHandle(id);

    switch (type) {
        case XP2PTypeClose:  // av recv close callback
            /* code */
            break;
        case XP2PTypeCmd:  // command request callback
            /* code */
            break;
        case XP2PTypeSaveFileOn:  //是否将音视频流保存成文件
            /* code */
            break;
        case XP2PTypeSaveFileUrl:  //文件存储路径
            /* code */
            break;
        case XP2PTypeLog:
            /* code */
            break;
        case XP2PTypeDisconnect:
            pHadnle->isReady = 0;
            break;
        case XP2PTypeDetectError:
            pHadnle->isReady = 0;
            break;
        case XP2PTypeDetectReady:
            pHadnle->isReady = 1;
            printf("%s p2p is ready!\n", id);
            break;
        case XP2PTypeStreamEnd:
            /* code */
            break;
        case XP2PTypeCmdNOReturn:
            /* code */
            break;
        default:
            break;
    }
    if (msgHandleProcCb)
        return msgHandleProcCb(id, type, msg);
    else
        return "";
}

void QcloudP2PRegister(av_recv_handle_t recv_handle, msg_handle_t msg_handle,
                       device_data_recv_handle_t device_data_handle, const char *sec_id,
                       const char *sec_key)
{
    msgHandleProcCb = msg_handle;
    setUserCallbackToXp2p(recv_handle, QcloudMsgProc, device_data_handle);
    setQcloudApiCred(sec_id, sec_key);
    //关闭日志
    setLogEnable(false, false);
}

void *QcloudP2PLanInt(const char *id, const char *product_id, const char *device_name,
                      const char *remote_host, const char *remote_port)
{
    int rc = 0;

    P2pApiHandle *handle = (P2pApiHandle *)QCLOUD_MALLOC(sizeof(P2pApiHandle));
    if (!handle) {
        printf("malloc buffer size %d failed\n", sizeof(P2pApiHandle));
        return NULL;
    }
    memset(handle, 0, sizeof(P2pApiHandle));
    strncpy(handle->id, id, sizeof(handle->id));
    handle->isLan = 1;
    QcloudSetHandle(handle);

    rc = startLanService(id, product_id, device_name, remote_host, remote_port);
    if (0 != rc) {
        printf("failed to init app lan p2p %d\n", rc);
        return NULL;
    }

    // wait p2p ready
    while (!handle->isReady) {
        __SYS_SLEEP_MS(100);
    }

    char *url = getLanUrl(id);
    if (!url) {
        printf("get lan url failed!\n");
        QcloudP2PExit(handle);
        return NULL;
    }

    handle->url = url;

    return handle;
}

void *QcloudP2PWanInt(const char *id, const char *product_id, const char *device_name,
                      const char *Xp2pInfo)
{
    int rc = 0;

    rc = startService(id, product_id, device_name, XP2P_PROTOCOL_AUTO);
    if (0 != rc) {
        printf("failed to init app p2p %d\n", rc);
        return NULL;
    }

    P2pApiHandle *handle = (P2pApiHandle *)QCLOUD_MALLOC(sizeof(P2pApiHandle));
    if (!handle) {
        printf("malloc buffer size %d failed\n", sizeof(P2pApiHandle));
        return NULL;
    }
    memset(handle, 0, sizeof(P2pApiHandle));
    strncpy(handle->id, id, sizeof(handle->id));
    QcloudSetHandle(handle);

    setDeviceXp2pInfo(id, Xp2pInfo);

    // wait p2p ready
    while (!handle->isReady) {
        __SYS_SLEEP_MS(100);
    }

    char *url = delegateHttpFlv(id);
    if (!url) {
        QcloudP2PExit(handle);
        return NULL;
    }
    handle->url = url;

    return handle;
}

void QcloudP2PExit(void *handle)
{
    P2pApiHandle *pHandle = (P2pApiHandle *)handle;
    if (pHandle) {
        stopService(pHandle->id);

        if (pHandle->LiveUrl) {
            QCLOUD_FREE(pHandle->LiveUrl);
            pHandle->LiveUrl = NULL;
        }

        if (pHandle->PlaybackUrl) {
            QCLOUD_FREE(pHandle->PlaybackUrl);
            pHandle->PlaybackUrl = NULL;
        }
        QcloudDelHandle(pHandle);
        QCLOUD_FREE(pHandle);
    }
}

const char *QcloudRequestLiveUrl(void *handle, const char *StreamType, int channel, bool crypto)
{
    P2pApiHandle *pHandle = (P2pApiHandle *)handle;
    if (!(pHandle && pHandle->url && StreamType)) {
        return NULL;
    }

    if (!pHandle->LiveUrl) {
        pHandle->LiveUrl = QCLOUD_MALLOC(MAX_URL_LENGTH);
        if (!pHandle->LiveUrl) {
            printf("malloc live url size %d failed\n", MAX_URL_LENGTH);
            return NULL;
        }
    }
    memset(pHandle->LiveUrl, 0, MAX_URL_LENGTH);

    int rc = 0;
    if (pHandle->isLan) {
        int port = getLanProxyPort(pHandle->id);
        if (port <= 0) {
            return NULL;
        }

        rc = QcloudSnprintf(pHandle->LiveUrl, MAX_URL_LENGTH - 1, QCLOUD_LAN_LIVE_URL, pHandle->url,
                            port, channel, StreamType);
    } else {
        rc = QcloudSnprintf(pHandle->LiveUrl, MAX_URL_LENGTH - 1, QCLOUD_WAN_LIVE_URL, pHandle->url,
                            channel, StreamType);
        if (!crypto) {
            strcat(pHandle->LiveUrl, "&_crypto=off");
        }
    }

    if (rc <= 0)
        return NULL;

    return pHandle->LiveUrl;
}

const char *QcloudRequestPlaybackUrl(void *handle, const char *StreamType, int channel, bool crypto,
                                     int start_time, int end_time)
{
    P2pApiHandle *pHandle = (P2pApiHandle *)handle;
    if (!(pHandle && pHandle->url && StreamType)) {
        return NULL;
    }

    if (!pHandle->PlaybackUrl) {
        pHandle->PlaybackUrl = QCLOUD_MALLOC(MAX_URL_LENGTH);
        if (!pHandle->PlaybackUrl) {
            printf("malloc playback url size %d failed\n", MAX_URL_LENGTH);
            return NULL;
        }
    }
    memset(pHandle->PlaybackUrl, 0, MAX_URL_LENGTH);

    int rc = 0;
    if (pHandle->isLan) {
        int port = getLanProxyPort(pHandle->id);
        if (port) {
            return NULL;
        }

        rc = QcloudSnprintf(pHandle->PlaybackUrl, MAX_URL_LENGTH - 1, QCLOUD_LAN_PLYABCAK_URL,
                            pHandle->url, port, start_time, end_time);
    } else {
        rc = QcloudSnprintf(pHandle->PlaybackUrl, MAX_URL_LENGTH - 1, QCLOUD_WAN_PLYABCAK_URL,
                            pHandle->url, start_time, end_time);
        if (!crypto) {
            strcat(pHandle->PlaybackUrl, "&_crypto=off");
        }
    }

    if (rc <= 0)
        return NULL;

    return pHandle->PlaybackUrl;
}

const int QcloudSendVoice(void *handle, int channel, bool crypto, uint8_t *data, size_t len)
{
    P2pApiHandle *pHandle = (P2pApiHandle *)handle;
    int rc                = 0;

    if (!pHandle) {
        return XP2P_ERR_CLIENT_NULL;
    }
    if (!pHandle->isSendService) {
        char params[20] = {0};
        QcloudSnprintf(params, sizeof(params) - 1, "channel=%d", channel);
        runSendService(pHandle->id, params, crypto);
        pHandle->isSendService = 1;
    }

    if (data && len) {
        rc = dataSend(pHandle->id, data, len);
    } else {
        rc = stopSendService(pHandle->id, NULL);
    }

    return rc;
}


const int QcloudSendVoiceCommand(void *handle, const char *cmd, bool crypto, uint8_t *data, size_t len)
{
    P2pApiHandle *pHandle = (P2pApiHandle *)handle;
    int rc                = 0;

    if (!pHandle) {
        return XP2P_ERR_CLIENT_NULL;
    }
    if (!pHandle->isSendService) {
        runSendService(pHandle->id, cmd, crypto);
        pHandle->isSendService = 1;
    }

    if (data && len) {
        rc = dataSend(pHandle->id, data, len);
    } else {
        rc = stopSendService(pHandle->id, NULL);
        pHandle->isSendService = 0;
    }

    return rc;
}


const int QcloudPostCommandRequest(void *handle, const unsigned char *command, size_t cmd_len,
                           unsigned char *recv_buf, uint64_t timeout_us)
{
    P2pApiHandle *pHandle = (P2pApiHandle *)handle;
    int rc                = 0;
    char *recv_ptr        = NULL;
    size_t recv_len       = 0;

    if (!pHandle) {
        return XP2P_ERR_CLIENT_NULL;
    }

    rc = postCommandRequestSync(pHandle->id, command, cmd_len, &recv_ptr, &recv_len, timeout_us);
    if (rc != 0){
        printf("command request failed! command: %s\n", command);
        return -1;
    }
    memcpy(recv_buf, recv_ptr, recv_len);
    free(recv_ptr);

    return recv_len;
}
