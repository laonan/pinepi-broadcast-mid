import asyncio, json, websockets

async def main():
    async with websockets.connect("wss://domain.com/ws/") as ws:
        # Step 1: authenticate
        await ws.send(json.dumps({"token": "yourtoken", "device": "epaper"}))

        # Step 2: receive messages
        async for message in ws:
            if isinstance(message, bytes):
                # Binary frame: 4000 bytes for 122x250 e-paper display
                if len(message) == 4000:
                    print("Displaying image on e-paper (Demo only)")
                else:
                    print(f"Unexpected binary data: {len(message)} bytes")
            else:
                data = json.loads(message)
                if data.get("type") == "ping":
                    await ws.send(json.dumps({"type": "pong"}))
                else:
                    print("Received:", data)

asyncio.run(main())
