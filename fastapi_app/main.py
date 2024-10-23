from fastapi import FastAPI, WebSocket
from fastapi.staticfiles import StaticFiles
import uvicorn
import asyncio
import pyaudio
import torch 
from transformers import AutoModelForSpeechSeq2Seq, AutoProcessor, pipeline
import numpy as np
import threading
from collections import deque
import logging
from pydantic import BaseModel

app = FastAPI()
logging.basicConfig(level=logging.WARN)
logger = logging.getLogger(__name__)

TRANSCRIPTION_MODEL_NAME = "openai/whisper-large-v3-turbo"

# Audio settings
STEP_IN_SEC: int = 1
LENGTH_IN_SEC: int = 7
NB_CHANNELS = 1
RATE = 16000
CHUNK = RATE

# Whisper settings
WHISPER_LANGUAGE = "en"
WHISPER_THREADS = 1

# Visualization
MAX_SENTENCE_CHARACTERS = 128

device_name = torch.device("cuda" if torch.cuda.is_available() else "mps" if torch.mps.is_available() else "cpu")
device = torch.device(device_name)
torch_dtype = torch.bfloat16

transcription_model = AutoModelForSpeechSeq2Seq.from_pretrained(
    TRANSCRIPTION_MODEL_NAME, torch_dtype=torch_dtype, low_cpu_mem_usage=True
)
transcription_model.to(device)

processor = AutoProcessor.from_pretrained(TRANSCRIPTION_MODEL_NAME)

transcription_pipeline = pipeline(
    "automatic-speech-recognition",
    model=transcription_model,
    tokenizer=processor.tokenizer,
    feature_extractor=processor.feature_extractor,
    chunk_length_s = min(LENGTH_IN_SEC, 30),
    torch_dtype=torch_dtype,
    device=device,
)

logger.info(f"{TRANSCRIPTION_MODEL_NAME} loaded")

global audio_buffer, START, RESUMING
audio_buffer = asyncio.Queue(maxsize=LENGTH_IN_SEC * CHUNK)
START = asyncio.Event()
RESUMING = False
active_connections = set()


class TranscriptionRequest(BaseModel):
    transcription: list

@app.post("/get_answers")
async def get_answers(request: TranscriptionRequest):
    transcription_history = request.transcription
    logger.info(f"Transcription History: {transcription_history}")
    answer = "This is a sample answer based on the provided transcription."
    return answer * (np.random.randint(5,50))

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    active_connections.add(websocket)
    try:
        while True:
            await websocket.receive_text()
    except:
        active_connections.remove(websocket)

async def send_transcription(transcription: str):
    for connection in active_connections:
        await connection.send_text(transcription)

async def producer_task():
    audio = pyaudio.PyAudio()
    stream = audio.open(
        format=pyaudio.paInt16,
        channels=NB_CHANNELS,
        rate=RATE,
        input=True,
        frames_per_buffer=CHUNK,
    )

    while START.is_set():
        try:
            audio_data = await asyncio.to_thread(stream.read, CHUNK, exception_on_overflow=False)
            await audio_buffer.put(audio_data)
            logger.info(f"Producer added data. Buffer size: {audio_buffer.qsize()}")
        except Exception as e:
            logger.error(f"Error in producer task: {str(e)}")

    stream.stop_stream()
    stream.close()
    audio.terminate()

async def transcribe(audio_data_array):
    return await asyncio.to_thread(
        transcription_pipeline,
        {"array": audio_data_array, "sampling_rate": RATE},
        return_timestamps=True,
        generate_kwargs={"language": "english", "return_timestamps": True, "max_new_tokens": MAX_SENTENCE_CHARACTERS}
    )

async def consumer_task():
    while START.is_set():
        try:
            if audio_buffer.qsize() >= LENGTH_IN_SEC:
                audio_data_to_process = b''.join([await audio_buffer.get() for _ in range(LENGTH_IN_SEC)])
                audio_data_array = np.frombuffer(audio_data_to_process, np.int16).astype(np.float32) / 32768.0

                transcription = await transcribe(audio_data_array)
                transcription_text = transcription["text"].rstrip(".")

                if transcription_text:
                    await send_transcription(transcription_text)
                    logger.info(f"Sent transcription: {transcription_text}")
            else:
                await asyncio.sleep(0.1)
        except Exception as e:
            logger.error(f"Error in consumer task: {str(e)}")

        logger.info(f"Consumer task iteration - Buffer size: {audio_buffer.qsize()}")

@app.post("/start")
async def start_transcription():
    global audio_buffer, RESUMING
    if not START.is_set():
        START.set()
        RESUMING = False
        # Clear the buffer by creating a new Queue
        audio_buffer = asyncio.Queue(maxsize=LENGTH_IN_SEC * CHUNK)
        asyncio.create_task(producer_task())
        asyncio.create_task(consumer_task())
    return {"status": "started"}

@app.post("/resume")
async def resume_transcription():
    if not START.is_set():
        START.set()
        global RESUMING
        RESUMING = True
        asyncio.create_task(producer_task())
        asyncio.create_task(consumer_task())
    return {"status": "resumed"}

@app.post("/stop")
async def stop_transcription():
    START.clear()
    return {"status": "stopped"}

async def status_check():
    while True:
        logger.info(f"Task status - START: {START.is_set()}, Buffer size: {audio_buffer.qsize()}")
        await asyncio.sleep(5)  # Check every 5 seconds

@app.on_event("startup")
async def startup_event():
    asyncio.create_task(status_check())

app.mount("/", StaticFiles(directory="static", html=True), name="static")
