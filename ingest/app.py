from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
import uvicorn

from ingest.contract import validate_message
from ingest.db import InMemoryStore, SampleStore


def handle_ingest(message: object, store: SampleStore) -> dict[str, int]:
    batch = validate_message(message)
    store.insert_batch(batch)
    return {"accepted": len(batch.samples)}


def create_app(store: SampleStore | None = None) -> FastAPI:
    app = FastAPI()
    store = store if store is not None else InMemoryStore()

    @app.post("/ingest")
    async def post_ingest(request: Request) -> dict[str, int]:
        try:
            return handle_ingest(await request.json(), store)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.websocket("/ingest/ws")
    async def websocket_ingest(websocket: WebSocket) -> None:
        await websocket.accept()
        try:
            while True:
                try:
                    result = handle_ingest(await websocket.receive_json(), store)
                except ValueError as exc:
                    await websocket.send_json({"error": str(exc)})
                else:
                    await websocket.send_json(result)
        except WebSocketDisconnect:
            pass

    return app


app = create_app()


def main() -> None:
    uvicorn.run(app, host="0.0.0.0", port=8000)


if __name__ == "__main__":
    main()
