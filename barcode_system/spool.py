"""
DB에 기록하지 못한 메시지를 임시로 보관하는 보조 파일 (JSON Lines, 한 줄 = 메시지 1건).

paho는 on_message 콜백이 끝나면 PUBACK을 보내므로(1.x는 응답을 늦출 방법이 없다),
DB가 잠겨 있거나 일시적으로 쓸 수 없을 때 메시지를 여기에 남겨 두고 나중에 DB로 옮긴다.
  append()  한 줄 추가 + fsync (전원이 꺼져도 남도록)
  drain()   처리 함수로 모두 넘기고, 성공한 줄은 지운다. 실패하면 그 줄부터 남긴다.
같은 메시지가 두 번 처리돼도 DB는 id로 중복을 제거하므로 안전하다.
"""
import base64
import json
import logging
import os
import threading

log = logging.getLogger("barcode.spool")


class Spool:
    def __init__(self, path):
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        self.path = path
        self._lock = threading.Lock()

    def append(self, topic, payload, received_at):
        line = json.dumps({"topic": topic, "payload": base64.b64encode(bytes(payload)).decode("ascii"),
                           "received_at": received_at}) + "\n"
        with self._lock:
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(line)
                f.flush()
                os.fsync(f.fileno())

    def __len__(self):
        with self._lock:
            return len(self._read())

    def _read(self):
        try:
            with open(self.path, encoding="utf-8") as f:
                return [line for line in f if line.strip()]
        except FileNotFoundError:
            return []

    def drain(self, handler):
        """handler(topic, payload, received_at)를 순서대로 호출한다. 처리한 개수를 반환.
        handler가 예외를 내면 거기서 멈추고 나머지를 파일에 남긴다."""
        with self._lock:
            lines = self._read()
            done = 0
            try:
                for line in lines:
                    try:
                        item = json.loads(line)
                        args = (item["topic"], base64.b64decode(item["payload"]), item["received_at"])
                    except (ValueError, KeyError, TypeError) as e:
                        # 쓰다가 전원이 꺼져 잘린 줄 등 → 되살릴 수 없으므로 내용과 함께 기록하고 건너뜀
                        log.error("보조 파일의 손상된 줄을 건너뜁니다: %r (%s)", line[:200], e)
                        done += 1
                        continue
                    handler(*args)
                    done += 1
            finally:
                if done:
                    self._rewrite(lines[done:])
            return done

    def _rewrite(self, remaining):
        if not remaining:
            os.remove(self.path)
            return
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.writelines(remaining)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, self.path)  # 원자적 교체: 도중에 꺼져도 이전 파일 또는 새 파일 중 하나가 남는다
