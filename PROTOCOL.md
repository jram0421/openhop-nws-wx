# Companion frame protocol notes (verified against openhop_core @ 54f6adb3)

Source: `companion/constants.py`, `companion/frame_server/{transport,commands_*,push}.py`.

## Framing
- TCP, loopback, one client (a new connection evicts the old one). No handshake, no auth.
- Client -> server: `0x3C` + u16 LE length + payload (payload = opcode + args). Length > `MAX_FRAME_SIZE` (176) disconnects us.
- Server -> client: `0x3E` + u16 LE length + payload. Heartbeat is `RESP_CODE_CURR_TIME` (9) + u32 LE time.
- Idle timeout (default 8h) is based on inbound frames, so send something periodically.

## Commands used
| Op | Payload |
|---|---|
| `CMD_APP_START` (1) | >= 7 bytes (reserved); reply `RESP_CODE_SELF_INFO` (5) |
| `CMD_DEVICE_QUERY` (22) | optional `data[0]` = app target version; send `3` to get V3 messages; reply DEVICE_INFO (13), `[3]` = max channels |
| `CMD_GET_CHANNEL` (31) | 1 byte index; reply `RESP_CODE_CHANNEL_INFO` (18): idx(1) name(32, NUL padded) secret(16). Empty name = unused slot |
| `CMD_SEND_CHANNEL_TXT_MSG` (3) | txt_type(1, 0=plain) chan_idx(1) timestamp(u32 LE) text(UTF-8); reply OK (0) / ERR (1)+code |
| `CMD_SYNC_NEXT_MESSAGE` (10) | none; reply `NO_MORE_MESSAGES` (10) or a message frame |

## Receiving
The server pushes only `PUSH_CODE_MSG_WAITING` (0x83), no payload. On it, loop `CMD_SYNC_NEXT_MESSAGE` until `NO_MORE_MESSAGES`.

`RESP_CODE_CHANNEL_MSG_RECV_V3` (17): `snr(1) 0 0 chan_idx(1) path_len(1) txt_type(1) timestamp(u32 LE) text`.
`RESP_CODE_CHANNEL_MSG_RECV` (8, legacy): `chan_idx path_len txt_type timestamp text`.
Direct messages (7 / 16) and channel data (27) can also arrive via sync and should be skipped.

Errors: 1 unsupported, 2 not found, 3 table full, 4 bad state, 5 file io, 6 illegal arg.

## Channel names
No lookup-by-name command. Enumerate indexes 0..max_channels-1 with `CMD_GET_CHANNEL` and match names client side.

## Open questions
- Exact `RESP_CODE_SENT` layout (not needed for channel send; reply is OK).
- Confirm on real hardware that `text` for channel messages includes the "sender: " prefix MeshCore uses.

## Plugin settings API
- `GET /api/plugins/settings?id=` and `POST /api/plugins/settings` (`{id, restart, config}`, full replace, 256 KiB cap).
- Auth: `X-API-Key` (admin-equivalent) or `Authorization: Bearer` JWT. Docs do not say a plugin page may use the dashboard session. Needs testing on a live Repeater; never embed an API key in the UI.
