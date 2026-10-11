"""
paho-mqtt 1.x / 2.x 호환 계층.

차이점
  생성자      : 2.x는 첫 인자로 CallbackAPIVersion이 필요하다 (없으면 경고 또는 오류)
  on_connect  : 1.x (client, userdata, flags, rc:int)
                2.x (client, userdata, flags, reason_code:ReasonCode, properties)
  on_disconnect: 1.x (client, userdata, rc)
                 2.x (client, userdata, disconnect_flags, reason_code, properties)
  on_publish  : 1.x (client, userdata, mid)
                2.x (client, userdata, mid, reason_code, properties)
이 모듈의 콜백은 버전과 관계없이 아래 형태로 호출된다.
  on_connect(ok: bool, reason: str) / on_disconnect(reason: str) / on_publish(mid: int)
"""
import paho.mqtt
import paho.mqtt.client as mqtt

PAHO_V2 = hasattr(mqtt, "CallbackAPIVersion")
PAHO_VERSION = getattr(paho.mqtt, "__version__", "?")

MQTT_ERR_SUCCESS = mqtt.MQTT_ERR_SUCCESS
MQTT_ERR_NO_CONN = mqtt.MQTT_ERR_NO_CONN


def make_client(client_id="", clean_session=True):
    if PAHO_V2:
        return mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=client_id,
                           clean_session=clean_session)
    return mqtt.Client(client_id=client_id, clean_session=clean_session)


def _rc_value(rc):
    """1.x의 int, 2.x의 ReasonCode 모두 정수 값으로."""
    return getattr(rc, "value", rc)


def _rc_text(rc):
    if PAHO_V2:
        return str(rc)  # ReasonCode는 "Success", "Not authorized" 등 이름으로 표시된다
    return mqtt.connack_string(rc) if isinstance(rc, int) else str(rc)


def set_callbacks(client, on_connect=None, on_disconnect=None, on_publish=None, on_message=None):
    if on_connect:
        if PAHO_V2:
            client.on_connect = lambda c, u, flags, rc, props=None: on_connect(
                _rc_value(rc) == 0, _rc_text(rc))
        else:
            client.on_connect = lambda c, u, flags, rc: on_connect(rc == 0, _rc_text(rc))
    if on_disconnect:
        if PAHO_V2:
            client.on_disconnect = lambda c, u, flags, rc, props=None: on_disconnect(str(rc))
        else:
            client.on_disconnect = lambda c, u, rc: on_disconnect(mqtt.error_string(rc))
    if on_publish:
        if PAHO_V2:
            client.on_publish = lambda c, u, mid, rc=None, props=None: on_publish(mid)
        else:
            client.on_publish = lambda c, u, mid: on_publish(mid)
    if on_message:  # 두 버전 모두 (client, userdata, message)
        client.on_message = lambda c, u, msg: on_message(msg)
