#ifndef __P2PAPI_H_
#define __P2PAPI_H_

#ifdef __cplusplus
extern "C" {
#endif

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#include "appWrapper.h"

#define STREAM_TYPE_STANDARD "standard"
#define STREAM_TYPE_HIGH     "high"
#define STREAM_TYPE_SUPER    "super"

void QcloudP2PRegister(av_recv_handle_t recv_handle, msg_handle_t msg_handle,
                       device_data_recv_handle_t device_data_handle, const char *sec_id,
                       const char *sec_key);

void *QcloudP2PLanInt(const char *id, const char *product_id, const char *device_name,
                      const char *remote_host, const char *remote_port);

void *QcloudP2PWanInt(const char *id, const char *product_id, const char *device_name,
                      const char *Xp2pInfo);

void QcloudP2PExit(void *handle);

const char *QcloudRequestLiveUrl(void *handle, const char *StreamType, int channel, bool crypto);

const char *QcloudRequestPlaybackUrl(void *handle, const char *StreamType, int channel, bool crypto,
                                     int start_time, int end_time);

const int QcloudSendVoice(void *handle, int channel, bool crypto, uint8_t *data, size_t len);

/* Send voice/custom data over the send-service opened with an explicit `cmd`
 * params string (e.g. "action=voice" per the Tencent XP2P SDK docs, which is
 * how the SDK knows to route the stream to the speaker's voice-decode path).
 * Calling with data==NULL closes the send service. */
const int QcloudSendVoiceCommand(void *handle, const char *cmd, bool crypto,
                                 uint8_t *data, size_t len);

const int QcloudPostCommandRequest(void *handle, const unsigned char *command, size_t cmd_len,
                           unsigned char *recv_buf, uint64_t timeout_us);

#ifdef WINDOWS
    #define __SYS_SLEEP_MS(x) Sleep((x))
#else
    #define __SYS_SLEEP_MS(x) usleep((x) * 1000)
#endif
#ifdef __cplusplus
}
#endif

#endif
