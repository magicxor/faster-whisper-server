from __future__ import annotations

import asyncio
import time
from io import BytesIO
from typing import Annotated, Generator, Iterable, Literal, OrderedDict

import gradio as gr
import huggingface_hub
from fastapi import (
    FastAPI,
    Form,
    HTTPException,
    Path,
    Query,
    Response,
    UploadFile,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.responses import StreamingResponse
from fastapi.websockets import WebSocketState
from faster_whisper import WhisperModel
from faster_whisper.transcribe import Segment, TranscriptionInfo
from faster_whisper.vad import VadOptions, get_speech_timestamps
from huggingface_hub.hf_api import ModelInfo
from pydantic import AfterValidator

from faster_whisper_server import utils
from faster_whisper_server.asr import FasterWhisperASR
from faster_whisper_server.audio import AudioStream, audio_samples_from_file
from faster_whisper_server.config import (
    SAMPLES_PER_SECOND,
    Language,
    ResponseFormat,
    Task,
    config,
)
from faster_whisper_server.gradio_app import create_gradio_demo
from faster_whisper_server.logger import logger
from faster_whisper_server.server_models import (
    ModelObject,
    TranscriptionJsonResponse,
    TranscriptionVerboseJsonResponse,
)
from faster_whisper_server.transcriber import audio_transcriber

loaded_models: OrderedDict[str, WhisperModel] = OrderedDict()


def load_model(model_name: str) -> WhisperModel:
    if model_name in loaded_models:
        logger.debug(f"{model_name} model already loaded")
        return loaded_models[model_name]
    if len(loaded_models) >= config.max_models:
        oldest_model_name = next(iter(loaded_models))
        logger.info(
            f"Max models ({config.max_models}) reached. Unloading the oldest model: {oldest_model_name}"
        )
        del loaded_models[oldest_model_name]
    logger.debug(f"Loading {model_name}...")
    start = time.perf_counter()
    # NOTE: will raise an exception if the model name isn't valid
    whisper = WhisperModel(
        model_name,
        device=config.whisper.inference_device,
        compute_type=config.whisper.compute_type,
    )
    logger.info(
        f"Loaded {model_name} loaded in {time.perf_counter() - start:.2f} seconds. {config.whisper.inference_device}({config.whisper.compute_type}) will be used for inference."
    )
    loaded_models[model_name] = whisper
    return whisper


app = FastAPI()


@app.get("/health")
def health() -> Response:
    return Response(status_code=200, content="OK")


@app.get("/v1/models")
def get_models() -> list[ModelObject]:
    models = huggingface_hub.list_models(library="ctranslate2")
    models = [
        ModelObject(
            id=model.id,
            created=int(model.created_at.timestamp()),
            object_="model",
            owned_by=model.id.split("/")[0],
        )
        for model in models
        if model.created_at is not None
    ]
    return models


@app.get("/v1/models/{model_name:path}")
# NOTE: `examples` doesn't work https://github.com/tiangolo/fastapi/discussions/10537
def get_model(
    model_name: Annotated[str, Path(example="Systran/faster-distil-whisper-large-v3")],
) -> ModelObject:
    models = list(
        huggingface_hub.list_models(model_name=model_name, library="ctranslate2")
    )
    if len(models) == 0:
        raise HTTPException(status_code=404, detail="Model doesn't exists")
    exact_match: ModelInfo | None = None
    for model in models:
        if model.id == model_name:
            exact_match = model
            break
    if exact_match is None:
        raise HTTPException(
            status_code=404,
            detail=f"Model doesn't exists. Possible matches: {", ".join([model.id for model in models])}",
        )
    assert exact_match.created_at is not None
    return ModelObject(
        id=exact_match.id,
        created=int(exact_match.created_at.timestamp()),
        object_="model",
        owned_by=exact_match.id.split("/")[0],
    )


def segments_to_response(
    segments: Iterable[Segment],
    transcription_info: TranscriptionInfo,
    response_format: ResponseFormat,
) -> str | TranscriptionJsonResponse | TranscriptionVerboseJsonResponse:
    segments = list(segments)
    if response_format == ResponseFormat.TEXT:
        return utils.segments_text(segments)
    elif response_format == ResponseFormat.JSON:
        return TranscriptionJsonResponse.from_segments(segments)
    elif response_format == ResponseFormat.VERBOSE_JSON:
        return TranscriptionVerboseJsonResponse.from_segments(
            segments, transcription_info
        )


def format_as_sse(data: str) -> str:
    return f"data: {data}\n\n"


def segments_to_streaming_response(
    segments: Iterable[Segment],
    transcription_info: TranscriptionInfo,
    response_format: ResponseFormat,
) -> StreamingResponse:
    def segment_responses() -> Generator[str, None, None]:
        for segment in segments:
            if response_format == ResponseFormat.TEXT:
                data = segment.text
            elif response_format == ResponseFormat.JSON:
                data = TranscriptionJsonResponse.from_segments(
                    [segment]
                ).model_dump_json()
            elif response_format == ResponseFormat.VERBOSE_JSON:
                data = TranscriptionVerboseJsonResponse.from_segment(
                    segment, transcription_info
                ).model_dump_json()
            yield format_as_sse(data)

    return StreamingResponse(segment_responses(), media_type="text/event-stream")


def handle_default_openai_model(model_name: str) -> str:
    """This exists because some callers may not be able override the default("whisper-1") model name.
    For example, https://github.com/open-webui/open-webui/issues/2248#issuecomment-2162997623.
    """
    if model_name == "whisper-1":
        logger.info(
            f"{model_name} is not a valid model name. Using {config.whisper.model} instead."
        )
        return config.whisper.model
    return model_name


ModelName = Annotated[str, AfterValidator(handle_default_openai_model)]


@app.post(
    "/v1/audio/translations",
    response_model=str | TranscriptionJsonResponse | TranscriptionVerboseJsonResponse,
)
def translate_file(
    file: Annotated[UploadFile, Form()],
    model: Annotated[ModelName, Form()] = config.whisper.model,
    prompt: Annotated[str | None, Form()] = None,
    response_format: Annotated[ResponseFormat, Form()] = config.default_response_format,
    temperature: Annotated[float, Form()] = 0.0,
    stream: Annotated[bool, Form()] = False,
) -> (
    str
    | TranscriptionJsonResponse
    | TranscriptionVerboseJsonResponse
    | StreamingResponse
):
    whisper = load_model(model)
    segments, transcription_info = whisper.transcribe(
        file.file,
        task=Task.TRANSLATE,
        initial_prompt=prompt,
        temperature=temperature,
        vad_filter=True,
    )

    if stream:
        return segments_to_streaming_response(
            segments, transcription_info, response_format
        )
    else:
        return segments_to_response(segments, transcription_info, response_format)


# https://platform.openai.com/docs/api-reference/audio/createTranscription
# https://github.com/openai/openai-openapi/blob/master/openapi.yaml#L8915
@app.post(
    "/v1/audio/transcriptions",
    response_model=str | TranscriptionJsonResponse | TranscriptionVerboseJsonResponse,
)
def transcribe_file(
    file: Annotated[UploadFile, Form()],
    model: Annotated[ModelName, Form()] = config.whisper.model,
    language: Annotated[Language | None, Form()] = config.default_language,
    prompt: Annotated[str | None, Form()] = None,
    response_format: Annotated[ResponseFormat, Form()] = config.default_response_format,
    temperature: Annotated[float, Form()] = 0.0,
    timestamp_granularities: Annotated[
        list[Literal["segment"] | Literal["word"]],
        Form(alias="timestamp_granularities[]"),
    ] = ["segment"],
    stream: Annotated[bool, Form()] = False,
) -> (
    str
    | TranscriptionJsonResponse
    | TranscriptionVerboseJsonResponse
    | StreamingResponse
):
    whisper = load_model(model)
    segments, transcription_info = whisper.transcribe(
        file.file,
        task=Task.TRANSCRIBE,
        language=language,
        initial_prompt=prompt,
        word_timestamps="word" in timestamp_granularities,
        temperature=temperature,
        vad_filter=True,
    )

    if stream:
        return segments_to_streaming_response(
            segments, transcription_info, response_format
        )
    else:
        return segments_to_response(segments, transcription_info, response_format)


async def audio_receiver(ws: WebSocket, audio_stream: AudioStream) -> None:
    try:
        while True:
            bytes_ = await asyncio.wait_for(
                ws.receive_bytes(), timeout=config.max_no_data_seconds
            )
            logger.debug(f"Received {len(bytes_)} bytes of audio data")
            audio_samples = audio_samples_from_file(BytesIO(bytes_))
            audio_stream.extend(audio_samples)
            if audio_stream.duration - config.inactivity_window_seconds >= 0:
                audio = audio_stream.after(
                    audio_stream.duration - config.inactivity_window_seconds
                )
                vad_opts = VadOptions(min_silence_duration_ms=500, speech_pad_ms=0)
                # NOTE: This is a synchronous operation that runs every time new data is received.
                # This shouldn't be an issue unless data is being received in tiny chunks or the user's machine is a potato.
                timestamps = get_speech_timestamps(audio.data, vad_opts)
                if len(timestamps) == 0:
                    logger.info(
                        f"No speech detected in the last {config.inactivity_window_seconds} seconds."
                    )
                    break
                elif (
                    # last speech end time
                    config.inactivity_window_seconds
                    - timestamps[-1]["end"] / SAMPLES_PER_SECOND
                    >= config.max_inactivity_seconds
                ):
                    logger.info(
                        f"Not enough speech in the last {config.inactivity_window_seconds} seconds."
                    )
                    break
    except asyncio.TimeoutError:
        logger.info(
            f"No data received in {config.max_no_data_seconds} seconds. Closing the connection."
        )
    except WebSocketDisconnect as e:
        logger.info(f"Client disconnected: {e}")
    audio_stream.close()


@app.websocket("/v1/audio/transcriptions")
async def transcribe_stream(
    ws: WebSocket,
    model: Annotated[ModelName, Query()] = config.whisper.model,
    language: Annotated[Language | None, Query()] = config.default_language,
    response_format: Annotated[
        ResponseFormat, Query()
    ] = config.default_response_format,
    temperature: Annotated[float, Query()] = 0.0,
) -> None:
    await ws.accept()
    transcribe_opts = {
        "language": language,
        "temperature": temperature,
        "vad_filter": True,
        "condition_on_previous_text": False,
    }
    whisper = load_model(model)
    asr = FasterWhisperASR(whisper, **transcribe_opts)
    audio_stream = AudioStream()
    try:
        async with asyncio.TaskGroup() as tg:
            tg.create_task(audio_receiver(ws, audio_stream))
            async for transcription in audio_transcriber(asr, audio_stream):
                logger.debug(f"Sending transcription: {transcription.text}")
                if ws.client_state == WebSocketState.DISCONNECTED:
                    break

                if response_format == ResponseFormat.TEXT:
                    await ws.send_text(transcription.text)
                elif response_format == ResponseFormat.JSON:
                    await ws.send_json(
                        TranscriptionJsonResponse.from_transcription(
                            transcription
                        ).model_dump()
                    )
                elif response_format == ResponseFormat.VERBOSE_JSON:
                    await ws.send_json(
                        TranscriptionVerboseJsonResponse.from_transcription(
                            transcription
                        ).model_dump()
                    )

    except WebSocketDisconnect as e:
        logger.info(f"Client disconnected: {e}")
    except Exception as e:
        logger.error(f"An error occurred: {e}")
    finally:
        if not audio_stream.closed:
            audio_stream.close()
        if ws.client_state != WebSocketState.DISCONNECTED:
            try:
                logger.info("Initiating close handshake.")
                await ws.close(code=1000)  # Normal closure
            except Exception as close_exception:
                logger.error(f"Error during close handshake: {close_exception}")



app = gr.mount_gradio_app(app, create_gradio_demo(config), path="/")
