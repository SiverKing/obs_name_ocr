# 版权所有 © 2026 www.siver.top
import asyncio
import base64
import hashlib
import json
import sys
import uuid

import websockets


async def request(ws, request_type, request_data=None):
    request_id = str(uuid.uuid4())
    await ws.send(
        json.dumps(
            {
                "op": 6,
                "d": {
                    "requestType": request_type,
                    "requestId": request_id,
                    "requestData": request_data or {},
                },
            },
            ensure_ascii=False,
        )
    )
    while True:
        msg = json.loads(await ws.recv())
        if msg.get("op") == 7 and msg.get("d", {}).get("requestId") == request_id:
            return msg["d"]


def build_auth(password, salt, challenge):
    secret = base64.b64encode(hashlib.sha256((password + salt).encode("utf-8")).digest())
    return base64.b64encode(hashlib.sha256(secret + challenge.encode("utf-8")).digest()).decode("ascii")


async def main():
    url = sys.argv[1] if len(sys.argv) > 1 else "ws://127.0.0.1:4455"
    password = sys.argv[2] if len(sys.argv) > 2 else ""

    async with websockets.connect(url, subprotocols=["obswebsocket.json"], max_size=None) as ws:
        hello = json.loads(await ws.recv())
        data = hello.get("d", {})
        identify = {"rpcVersion": min(int(data.get("rpcVersion", 1)), 1), "eventSubscriptions": 0}
        auth = data.get("authentication")
        if auth:
            if not password:
                raise SystemExit("OBS WebSocket requires a password. Pass it as the second argument.")
            identify["authentication"] = build_auth(password, auth["salt"], auth["challenge"])

        await ws.send(json.dumps({"op": 1, "d": identify}, ensure_ascii=False))
        await ws.recv()

        scenes = await request(ws, "GetSceneList")
        inputs = await request(ws, "GetInputList")

        print("Scenes:")
        for scene in scenes["responseData"]["scenes"]:
            print(f"  {scene['sceneName']} | {scene['sceneUuid']}")

        print("Inputs:")
        for item in inputs["responseData"]["inputs"]:
            print(f"  {item['inputName']} | {item['inputKind']} | {item['inputUuid']}")


if __name__ == "__main__":
    asyncio.run(main())
