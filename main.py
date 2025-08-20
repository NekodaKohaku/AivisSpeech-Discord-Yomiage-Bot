import discord
from discord import app_commands
import re
import asyncio
import yaml
import random
import aiohttp
import os
import uuid
import shutil
import emoji
import wave
import contextlib
from typing import Optional, Dict
from src import config as cfg_module
from src import guild_tts_manager as tts_manager_module

# ===============================
# 基本ディレクトリとグローバル設定
# ===============================
SAVED_WAV_DIR = 'saved_wav'
TEMP_WAV_DIR = os.path.join('temp', 'wav')
os.makedirs(SAVED_WAV_DIR, exist_ok=True)
os.makedirs(TEMP_WAV_DIR, exist_ok=True)
PER_REQUEST_TIMEOUT = 10.0
FIRST_REPLY_TIMEOUT = 10.0
CUSTOM_EMOJI_RE = re.compile(r'<a?:(\w+):(\d+)>')
URL_RE = re.compile(r'https?://[^\s]+')

# ===============================
# 音声（キャラクター）ID 定義
# ===============================
available_voice_ids = [
    {"name": "Anneli", "id": 888753760}
]

VOICE_ID_SET = {v["id"] for v in available_voice_ids}
VOICE_ID_NAME = {v["id"]: v["name"] for v in available_voice_ids}

# ===============================
# ユーザー別の音声マッピング
# ===============================
USER_VOICE_MAPPING_FILE = "voice_mapping.yaml"
user_voice_mapping: Dict[int, dict] = {}

def load_voice_mapping():
    global user_voice_mapping
    if os.path.exists(USER_VOICE_MAPPING_FILE):
        with open(USER_VOICE_MAPPING_FILE, "r") as f:
            user_voice_mapping = yaml.safe_load(f) or {}
    else:
        user_voice_mapping = {}

_save_pending = False
def save_voice_mapping_debounced(delay: float = 0.8):
    global _save_pending
    if _save_pending:
        return
    _save_pending = True

    async def _later():
        global _save_pending
        try:
            await asyncio.sleep(delay)
            with open(USER_VOICE_MAPPING_FILE, "w") as f:
                yaml.safe_dump(user_voice_mapping, f, allow_unicode=True)
        finally:
            _save_pending = False

    try:
        asyncio.get_running_loop().create_task(_later())
    except RuntimeError:
        with open(USER_VOICE_MAPPING_FILE, "w") as f:
            yaml.safe_dump(user_voice_mapping, f, allow_unicode=True)

load_voice_mapping()

def get_random_voice_id() -> int:
    return random.choice(available_voice_ids)['id']

def get_voice_for_user(user_id: int, display_name: str) -> int:
    if user_id in user_voice_mapping:
        user_data = user_voice_mapping[user_id]
        if isinstance(user_data, dict):
            return user_data.get('voice_id', 888753760)
        user_voice_mapping[user_id] = {'voice_id': int(user_data), 'display_name': display_name}
        save_voice_mapping_debounced()
        return int(user_data)

    voice_id = get_random_voice_id()
    user_voice_mapping[user_id] = {'voice_id': voice_id, 'display_name': display_name}
    save_voice_mapping_debounced()
    return voice_id

# ===============================
# Discord 用 WAV フォーマット検査
# ===============================
DISCORD_SR = 48000
DISCORD_CH = 2
DISCORD_SW = 2

def probe_wav(path: str):
    """WAV を開いて (sr, ch, sampwidth_bytes, nframes) を返す（非WAVは例外）"""
    with contextlib.closing(wave.open(path, 'rb')) as w:
        return (w.getframerate(), w.getnchannels(), w.getsampwidth(), w.getnframes())

def is_discord_wav(sr: int, ch: int, sw: int) -> bool:
    """Discord 再生向けの想定形式かどうかを判定"""
    return (sr == DISCORD_SR and ch == DISCORD_CH and sw == DISCORD_SW)

# ===============================
# PCM 直接再生用 AudioSource
# ===============================
FRAME_SAMPLES = 960
CHANNELS = 2
BYTES_PER_SAMPLE = 2
FRAME_BYTES = FRAME_SAMPLES * CHANNELS * BYTES_PER_SAMPLE  # 960*2*2=3840 bytes

class BytesWavPCMSource(discord.AudioSource):
    def __init__(self, wav_bytes: bytes):
        import io
        self._bio = io.BytesIO(wav_bytes)
        self._wav = wave.open(self._bio, 'rb')

        sr = self._wav.getframerate()
        ch = self._wav.getnchannels()
        sw = self._wav.getsampwidth()

        if sr != 48000:
            raise ValueError("WAV must be 48kHz")
        if sw != 2:
            raise ValueError("WAV must be 16-bit (s16)")
        if ch not in (1, 2):
            raise ValueError("WAV must be mono or stereo (1ch/2ch)")

        self._channels = ch
        self._padded_last = False

    def read(self) -> bytes:
        if self._padded_last:
            return b''

        raw = self._wav.readframes(FRAME_SAMPLES)
        if not raw:
            return b''

        if self._channels == 2:
            frame_bytes = FRAME_BYTES
            if len(raw) < frame_bytes:
                raw += b'\x00' * (frame_bytes - len(raw))
                self._padded_last = True
            return raw

        mono_frame_bytes = FRAME_SAMPLES * BYTES_PER_SAMPLE  # 1920
        if len(raw) < mono_frame_bytes:
            raw += b'\x00' * (mono_frame_bytes - len(raw))
            self._padded_last = True

        out = bytearray(FRAME_BYTES)
        mv_in = memoryview(raw)
        mv_out = memoryview(out)
        for i in range(FRAME_SAMPLES):
            s = mv_in[2*i:2*i+2]
            mv_out[4*i:4*i+2] = s
            mv_out[4*i+2:4*i+4] = s
        return mv_out.tobytes()

    def is_opus(self) -> bool:
        return False

    def cleanup(self):
        try:
            self._wav.close()
        except:
            pass
        self._bio = None
        self._wav = None

def build_audio_entry_from_bytes(wav_bytes: bytes, volume: float = 1.0):
    source = BytesWavPCMSource(wav_bytes)
    source = discord.PCMVolumeTransformer(source, volume=volume)
    return {
        "audio": source,
        "file_path": None,
        "delete_after_play": False,
        "debug_used": "PCM(mem-upmix)"
    }

class WavPCMSource(discord.AudioSource):
    def __init__(self, wav_path: str):
        self.path = wav_path
        self._wav = wave.open(wav_path, 'rb')

        if self._wav.getframerate() != 48000:
            raise ValueError("WAV must be 48kHz")
        if self._wav.getsampwidth() != 2:
            raise ValueError("WAV must be 16-bit (s16)")
        if self._wav.getnchannels() != 2:
            raise ValueError("WAV must be stereo (2ch)")

        self._padded_last = False

    def read(self) -> bytes:
        if self._padded_last:
            return b''

        data = self._wav.readframes(FRAME_SAMPLES)
        if not data:
            return b''

        if len(data) < FRAME_BYTES:
            data += b'\x00' * (FRAME_BYTES - len(data))
            self._padded_last = True

        return data

    def is_opus(self) -> bool:
        return False

    def cleanup(self):
        try:
            self._wav.close()
        except:
            pass

def build_audio_entry(wav_path: str, saved_dir: str = SAVED_WAV_DIR, volume: float = 1.0):
    try:
        source = WavPCMSource(wav_path)
        source = discord.PCMVolumeTransformer(source, volume=volume)
        used = "PCM"

        delete_after_play = not os.path.abspath(wav_path).startswith(os.path.abspath(saved_dir))
        return {
            "audio": source,
            "file_path": wav_path,
            "delete_after_play": delete_after_play,
            "debug_used": used
        }
    except Exception as e:
        print(f"[TTS][error] build_audio_entry 失敗 {wav_path}: {e}")
        raise

# ===============================
# HTTP セッション共有（TTS 用）
# ===============================
_http_session: Optional[aiohttp.ClientSession] = None

async def get_http_session() -> aiohttp.ClientSession:
    global _http_session
    if _http_session is None or _http_session.closed:
        _http_session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=15),
            connector=aiohttp.TCPConnector(
                limit=100,
                limit_per_host=50,
                ttl_dns_cache=300,
                ssl=False,
                keepalive_timeout=60,
                enable_cleanup_closed=True
            ),
            trust_env=False,
        )
    return _http_session

# ===============================
# TTS 生成
# ===============================
def probe_wav_bytes(data: bytes):
    import io
    with contextlib.closing(wave.open(io.BytesIO(data), 'rb')) as w:
        return w.getframerate(), w.getnchannels(), w.getsampwidth(), w.getnframes()

async def generate_wav_bytes_from_server(text: str, speaker: int, host: str, port: int) -> Optional[bytes]:
    session = await get_http_session()
    req_to = aiohttp.ClientTimeout(total=PER_REQUEST_TIMEOUT)
    params = {'text': text, 'speaker': speaker, "enable_interrogative_upspeak": "true"}

    try:
        async with session.post(
            f'http://{host}:{port}/audio_query',
            params=params,
            timeout=req_to
        ) as resp_query:
            resp_query.raise_for_status()
            query_data = await resp_query.json()

        query_data["outputSamplingRate"] = 48000
        query_data["outputStereo"] = False
        query_data["leading_silence_seconds"] = 0.0
        
        headers = {'Content-Type': 'application/json'}
        async with session.post(
            f'http://{host}:{port}/synthesis',
            headers=headers,
            params=params,
            json=query_data,
            timeout=req_to
        ) as resp_synth:
            resp_synth.raise_for_status()
            audio_data = await resp_synth.read()

        sr, ch, sw, _ = probe_wav_bytes(audio_data)
        if sr != 48000 or sw != 2 or ch not in (1, 2):
            raise ValueError(f"不正なWAV形式: sr={sr}, ch={ch}, sw={sw*8}bit")
        return audio_data

    except Exception as e:
        print(f"Error generating wav(bytes) from {host}:{port} - {e}")
        return None

async def generate_wav_bytes(text: str, speaker: int = 888753760) -> Optional[bytes]:
    servers = [
        {"host": "localhost", "port": 10101},
        {"host": "192.168.0.246", "port": 10101},
    ]
    tasks = [asyncio.create_task(generate_wav_bytes_from_server(text, speaker, s["host"], s["port"])) for s in servers]

    winner: Optional[bytes] = None
    try:
        for coro in asyncio.as_completed(tasks, timeout=FIRST_REPLY_TIMEOUT):
            try:
                result = await coro
            except Exception:
                continue
            if result:
                winner = result
                break
    except asyncio.TimeoutError:
        winner = None
    finally:
        for t in tasks:
            if not t.done():
                t.cancel()
    return winner

async def generate_wav_from_server(text: str, speaker: int, filepath: str, host: str, port: int) -> Optional[str]:
    session = await get_http_session()

    async def _do_one():
        params = {'text': text, 'speaker': speaker, "enable_interrogative_upspeak": "true"}
        req_to = aiohttp.ClientTimeout(total=PER_REQUEST_TIMEOUT)

        async with session.post(
            f'http://{host}:{port}/audio_query',
            params=params,
            timeout=req_to
        ) as resp_query:
            resp_query.raise_for_status()
            query_data = await resp_query.json()

        query_data["outputSamplingRate"] = 48000
        query_data["outputStereo"] = True
        query_data["leading_silence_seconds"] = 0.0

        headers = {'Content-Type': 'application/json'}
        async with session.post(
            f'http://{host}:{port}/synthesis',
            headers=headers,
            params=params,
            json=query_data,
            timeout=req_to
        ) as resp_synth:
            resp_synth.raise_for_status()
            audio_data = await resp_synth.read()

        with open(filepath, "wb") as f:
            f.write(audio_data)

        sr, ch, sw, _ = probe_wav(filepath)
        if not is_discord_wav(sr, ch, sw):
            raise ValueError(f"不正なWAV形式: sr={sr}, ch={ch}, sw={sw*8}bit（48kHz/2ch/16bit が必須）")

        return filepath

    try:
        return await asyncio.wait_for(_do_one(), timeout=PER_REQUEST_TIMEOUT)
    except Exception as e:
        try:
            if os.path.exists(filepath):
                os.remove(filepath)
        except:
            pass
        print(f"Error generating wav from {host}:{port} - {e}")
        return None

async def generate_wav(text: str, speaker: int = 888753760, file_dir: str = TEMP_WAV_DIR) -> Optional[str]:
    os.makedirs(file_dir, exist_ok=True)

    servers = [
        {"host": "localhost", "port": 10101},
    ]

    tasks = []
    paths = []

    for s in servers:
        p = os.path.join(file_dir, f"{uuid.uuid4()}.wav")
        paths.append(p)
        tasks.append(asyncio.create_task(
            generate_wav_from_server(text, speaker, p, s["host"], s["port"])
        ))

    winner: Optional[str] = None

    try:
        for coro in asyncio.as_completed(tasks, timeout=FIRST_REPLY_TIMEOUT):
            try:
                result = await coro
            except Exception:
                continue
            if result and os.path.exists(result):
                winner = result
                break
    except asyncio.TimeoutError:
        winner = None
    finally:
        for t in tasks:
            if not t.done():
                t.cancel()
        for p in paths:
            if p != winner and os.path.exists(p):
                try:
                    os.remove(p)
                except:
                    pass

    return winner

# ===============================
# 通知音声（入室・退室）の生成
# ===============================
async def generate_notification_wav(action: str, user, speaker: int = 888753760) -> Optional[str]:
    user_id = int(user.id)
    display_name = user.display_name

    filename = f"{action}_{user.guild.id}_{user_id}.wav"
    filepath = os.path.join(SAVED_WAV_DIR, filename)

    rec = user_voice_mapping.get(user_id)
    if rec is None:
        rec = {"display_name": display_name, "voice_id": get_random_voice_id()}
        user_voice_mapping[user_id] = rec
        save_voice_mapping_debounced()

    if os.path.exists(filepath) and rec.get("display_name") == display_name:
        return filepath

    if rec.get("display_name") != display_name:
        rec["display_name"] = display_name
        save_voice_mapping_debounced()

    text = f"{display_name} さんが{'入室' if action == 'join' else '退室'}しました。"

    temp_wav = await generate_wav(text, speaker)
    if temp_wav and os.path.exists(temp_wav):
        try:
            os.replace(temp_wav, filepath)
        except Exception:
            shutil.move(temp_wav, filepath)
        return filepath

    return None

# ===============================
# Discord Bot（Client 拡張）
# ===============================
class Bot(discord.Client):
    async def setup_hook(self) -> None:
        """起動時：アプリコマンド同期 & 共有 HTTP セッション初期化"""
        await tree.sync()
        await get_http_session()

    async def close(self) -> None:
        """終了時：共有 HTTP セッションをクローズ"""
        global _http_session
        try:
            if _http_session is not None and not _http_session.closed:
                await _http_session.close()
        finally:
            _http_session = None
            await super().close()

config_obj = cfg_module.Config()
tts_manager = tts_manager_module.guild_tts_manager()

discord_access_token = config_obj.discord_access_token
discord_application_id = config_obj.discord_application_id

intents = discord.Intents.all()
client = Bot(intents=intents)
tree = app_commands.CommandTree(client)

# ===============================
# VoiceGuard：安全再接続ユーティリティ
# ===============================
_restart_locks: Dict[int, asyncio.Lock] = {}
_backoff_state: Dict[int, int] = {}  # guild.id -> backoff step

def guild_lock(guild_id: int) -> asyncio.Lock:
    return _restart_locks.setdefault(guild_id, asyncio.Lock())

async def restart_voice(guild: discord.Guild):
    vc = guild.voice_client
    lock = _restart_locks.setdefault(guild.id, asyncio.Lock())
    async with lock:
        step = _backoff_state.get(guild.id, 0)
        delay = min(1 * (2 ** step), 30)
        _backoff_state[guild.id] = min(step + 1, 5)

        try:
            me_vs = getattr(guild.me, "voice", None)
            ch = getattr(vc, "channel", None) if vc else (getattr(me_vs, "channel", None) if me_vs else None)

            print(f"[VOICE] restart begin guild={guild.id} ghost={vc is None and me_vs and me_vs.channel is not None} backoff={delay}s")

            if vc and vc.is_connected():
                try:
                    await vc.disconnect(force=True)
                except Exception as e:
                    print(f"[VOICE] disconnect error guild={guild.id}: {e}")
            else:
                await force_drop_voice(guild)

            await asyncio.sleep(delay)

            if ch is not None:
                await safe_connect(ch)
                print(f"[VOICE] restart done guild={guild.id}")
            else:
                print(f"[VOICE] restart skipped (no channel) guild={guild.id}")

        finally:
            async def _cooldown():
                await asyncio.sleep(120)
                _backoff_state[guild.id] = 0
            asyncio.create_task(_cooldown())

async def force_drop_voice(guild: discord.Guild):
    try:
        await guild.change_voice_state(channel=None, self_mute=False, self_deaf=False)
        print(f"[VOICE] force_drop_voice done guild={guild.id}")
    except Exception as e:
        print(f"[VOICE] force_drop_voice error guild={guild.id}: {e}")

async def safe_connect(channel: discord.VoiceChannel, timeout: float = 8.0, retries: int = 2):
    for i in range(retries + 1):
        try:
            return await asyncio.wait_for(channel.connect(reconnect=True), timeout=timeout)
        except asyncio.TimeoutError:
            print(f"[VOICE] connect timeout ch={channel.id} try={i+1}")
            await force_drop_voice(channel.guild)
            await asyncio.sleep(1 + i)
        except discord.ClientException as e:
            print(f"[VOICE] connect ClientException ch={channel.id} try={i+1} e={e}")
            await force_drop_voice(channel.guild)
            await asyncio.sleep(1 + i)
    raise RuntimeError("safe_connect: すべての再試行に失敗しました")

# ===============================
# /vrestart（音声再起動）
# ===============================
@tree.command(
    name="vrestart",
    description="このサーバーの音声接続を再起動します（強制切断→再接続）"
)
@app_commands.checks.cooldown(rate=1, per=30.0, key=lambda i: i.guild_id)  # ギルドごと 30 秒に 1 回
async def vrestart_command(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)

    guild = interaction.guild
    vc = guild.voice_client
    me_vs = getattr(guild.me, "voice", None)
    ghost = (vc is None) and (me_vs is not None) and (me_vs.channel is not None)
    user_ch = getattr(interaction.user.voice, "channel", None)
    actor = interaction.user.display_name

    try:
        # 1) 幽霊：優先してユーザーのVCへ。なければ幽霊VCへ戻る
        if ghost:
            target_ch = user_ch if user_ch else me_vs.channel
            await force_drop_voice(guild)
            await asyncio.sleep(0.5)
            async with guild_lock(guild.id):
                await safe_connect(target_ch)

            await interaction.channel.send(f"**{actor}**さんが音声接続を再起動しました。")

            # ユーザー不在で幽霊チャンネルに戻った場合、無人なら退出
            if not user_ch:
                non_bot = [m for m in target_ch.members if not m.bot]
                if not non_bot and guild.voice_client:
                    await guild.voice_client.disconnect()
                    await interaction.followup.send(
                        "再接続先に一般メンバーがいないため、ボイスチャンネルから退出しました。",
                        ephemeral=True
                    )
                    return

            await interaction.followup.send("音声接続を再確立しました。", ephemeral=True)
            return

        # 2) 既に VoiceClient がある（幽霊ではない）
        if vc and vc.is_connected():
            if user_ch and vc.channel != user_ch:
                async with guild_lock(guild.id):
                    await safe_connect(user_ch)
            else:
                await restart_voice(guild)

            await interaction.channel.send(f"**{actor}**さんが音声接続を再起動しました。")
            await interaction.followup.send("音声接続を再確立しました。", ephemeral=True)
            return

        # 3) 完全に未接続
        if not user_ch:
            await interaction.followup.send(
                "現在、ボイスチャンネルに接続していません。先にボイスチャンネルに参加してから再実行してください。",
                ephemeral=True
            )
            return

        await safe_connect(user_ch)
        await interaction.channel.send(f"**{actor}**さんが音声接続を再起動しました。")
        await interaction.followup.send("音声チャンネルに接続しました。", ephemeral=True)

    except Exception as e:
        await interaction.followup.send(f"再起動に失敗しました: {e}", ephemeral=True)

@vrestart_command.error
async def vrestart_command_error(interaction: discord.Interaction, error: Exception):
    """クールダウンなどの例外をユーザーに丁寧に通知"""
    if isinstance(error, app_commands.CommandOnCooldown):
        retry = getattr(error, "retry_after", 0.0)
        msg = f"このコマンドはクールダウン中です。あと {retry:.1f} 秒お待ちください。"
        if not interaction.response.is_done():
            await interaction.response.send_message(msg, ephemeral=True)
        else:
            await interaction.followup.send(msg, ephemeral=True)
    else:
        if not interaction.response.is_done():
            await interaction.response.send_message("コマンド実行中にエラーが発生しました。", ephemeral=True)
        else:
            await interaction.followup.send("コマンド実行中にエラーが発生しました。", ephemeral=True)

# ===============================
# /vjoin（ボイスに参加）
# ===============================
@tree.command(name="vjoin", description="ボイスチャンネルにボットを参加させます。")
async def join_command(interaction: discord.Interaction):
    if interaction.user.voice is None:
        await interaction.response.send_message("先にボイスチャンネルへ参加してください。", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)

    guild = interaction.guild
    target_ch = interaction.user.voice.channel
    vc = guild.voice_client

    try:
        async with guild_lock(guild.id):
            if vc and vc.is_connected():
                if vc.channel.id != target_ch.id:
                    await safe_connect(target_ch)
            else:
                await force_drop_voice(guild)
                await asyncio.sleep(0.5)
                await safe_connect(target_ch)

        await interaction.followup.send("ボイスチャンネルに接続しました。", ephemeral=True)
        wav_path = os.path.abspath(os.path.join(SAVED_WAV_DIR, 'bot_join.wav'))
        tts_manager.enqueue(guild.voice_client, guild, build_audio_entry(wav_path))
    except Exception as e:
        await interaction.followup.send(f"接続に失敗しました: {e}", ephemeral=True)

# ===============================
# /vleave（ボイスから切断）
# ===============================
@tree.command(name="vleave", description="ボイスチャンネルからボットを切断します。")
async def leave_command(interaction: discord.Interaction):
    guild = interaction.guild
    vc = guild.voice_client
    me_vs = getattr(guild.me, "voice", None)
    user_vs = getattr(interaction.user.voice, "channel", None)

    await interaction.response.defer(ephemeral=True)

    try:
        if me_vs and me_vs.channel and user_vs != me_vs.channel:
            await interaction.followup.send("ボットのいるチャンネルでコマンドを実行してください。", ephemeral=True)
            return

        async with guild_lock(guild.id):
            if vc and vc.is_connected():
                try:
                    await vc.disconnect()
                except Exception:
                    await vc.disconnect(force=True)
                await interaction.followup.send("切断しました。", ephemeral=True)
            elif me_vs and me_vs.channel is not None:
                await force_drop_voice(guild)
                await interaction.followup.send("接続が不整合のため、強制的に切断要求を送りました。", ephemeral=True)
            else:
                await interaction.followup.send("ボットはボイスチャンネルに接続していません。", ephemeral=True)
    except Exception as e:
        await interaction.followup.send(f"切断に失敗しました: {e}", ephemeral=True)

# ===============================
# /list_voices・/set_voice
# ===============================
@tree.command(name="list_voices", description="利用可能な声線を一覧表示します。")
async def list_voices_command(interaction: discord.Interaction):
    user_id = interaction.user.id
    user_data = user_voice_mapping.get(user_id, {})
    current_voice_id = user_data.get('voice_id')
    lines = ["キャラクター名\tID"]
    for v in available_voice_ids:
        mark = " （現在の設定）" if v['id'] == current_voice_id else ""
        lines.append(f"{v['name']}\t{v['id']}{mark}")
    await interaction.response.send_message(content="\n".join(lines), ephemeral=True)

@tree.command(name="set_voice", description="自分の声線を設定します。")
async def set_voice_command(interaction: discord.Interaction, voice_id: int):
    user_id = interaction.user.id
    current_display_name = interaction.user.display_name
    if voice_id not in VOICE_ID_SET:
        await interaction.response.send_message("無効な声線 ID です。/list_voices で確認してください。", ephemeral=True)
        return

    user_voice_mapping[user_id] = {'voice_id': voice_id, 'display_name': current_display_name}
    save_voice_mapping_debounced()
    voice_name = VOICE_ID_NAME.get(voice_id, "不明な声線")
    await interaction.response.send_message(f"あなたの声線を (ID: {voice_id} {voice_name}) に設定しました。", ephemeral=True)

# ===============================
# TTS 生成 & 再生
# ===============================
@client.event
async def on_message(message: discord.Message):
    # Bot / DM は無視
    if message.author.bot or message.guild is None:
        return

    vc = message.guild.voice_client
    if vc is None:
        return

    # （運用方針により）テキストチャンネル ID と ボイスチャンネル ID を一致要求
    if message.channel.id != vc.channel.id:
        return

    # 1) 添付ファイルがある場合：固定効果音 attachment.wav を再生して終了
    if message.attachments:
        attach_wav = os.path.abspath(os.path.join(SAVED_WAV_DIR, 'attachment.wav'))
        if os.path.exists(attach_wav):
            tts_manager.enqueue(vc, message.guild, build_audio_entry(attach_wav))
        return

    # 2) メッセージ本文（空や空白のみなら終了）
    original_content = (message.content or "").strip()
    if not original_content:
        return

    # 3) URL のみ → 固定音声 url.wav（早期リターン）
    if re.fullmatch(URL_RE, original_content):
        url_wav = os.path.abspath(os.path.join(SAVED_WAV_DIR, 'url.wav'))
        if os.path.exists(url_wav):
            tts_manager.enqueue(vc, message.guild, build_audio_entry(url_wav))
        return

    # 4) ユーザーの声線 ID を取得（未登録ならランダム付与）
    speaker_id = get_voice_for_user(message.author.id, message.author.display_name)

    # 5) テキスト整形：カスタム絵文字・既存絵文字を除去
    content = CUSTOM_EMOJI_RE.sub('', original_content)
    content = emoji.replace_emoji(content, replace="")

    # 6) "neko!" で始まるメッセージ（Music Bot 用）は無視
    if content.lower().startswith("neko!"):
        return

    # 7) メンションを表示名に変換
    for mentioned in message.mentions:
        content = content.replace(f"<@{mentioned.id}>", f"アットマーク {mentioned.display_name}")
    for role in message.role_mentions:
        content = content.replace(f"<@&{role.id}>", f"アットマーク {role.name}")

    # 8) テキスト中の URL を固定語「URL」に置換
    content = URL_RE.sub("URL", content)

    # 9) 長文は上限で切り詰め
    if len(content) > config_obj.max_text_length:
        content = content[:config_obj.max_text_length] + "以下省略"

    # 10) TTS 生成（毎回新規に合成。キャッシュ・in-flight なし）
    wav_bytes: Optional[bytes] = await generate_wav_bytes(content, speaker_id)

    # 11) 成功したら再生キューへ投入（TEMP 生成物は再生後に自動削除）
    if wav_bytes:
        tts_manager.enqueue(vc, message.guild, build_audio_entry_from_bytes(wav_bytes))

# ===============================
# ボイス状態イベント
# ===============================
@client.event
async def on_voice_state_update(member: discord.Member, before: discord.VoiceState, after: discord.VoiceState):
    guild = member.guild
    vc = guild.voice_client
    me_vs = getattr(guild.me, "voice", None)

    ghost = (vc is None and me_vs and me_vs.channel is not None)
    if ghost:
        await restart_voice(guild)
        return

    if before.channel is not None and after.channel is not None and before.channel != after.channel:
        if vc is not None:
            if after.channel == vc.channel:
                if not member.bot:
                    wav_path = await generate_notification_wav("join", member, speaker=888753760)
                    if wav_path:
                        tts_manager.enqueue(vc, guild, build_audio_entry(wav_path))
            elif before.channel == vc.channel:
                if not member.bot:
                    wav_path = await generate_notification_wav("leave", member, speaker=888753760)
                    if wav_path:
                        tts_manager.enqueue(vc, guild, build_audio_entry(wav_path))
        else:
            async with guild_lock(guild.id):
                await safe_connect(after.channel)
            vc = guild.voice_client
            bot_join_path = os.path.abspath(os.path.join(SAVED_WAV_DIR, 'bot_join.wav'))
            tts_manager.enqueue(vc, guild, build_audio_entry(bot_join_path))

    if before.channel is None and after.channel is not None:
        if guild.voice_client is not None:
            if after.channel != guild.voice_client.channel:
                return
        else:
            async with guild_lock(guild.id):
                await safe_connect(after.channel)
            vc = guild.voice_client
            bot_join_path = os.path.abspath(os.path.join(SAVED_WAV_DIR, 'bot_join.wav'))
            tts_manager.enqueue(vc, guild, build_audio_entry(bot_join_path))
            return

        if not member.bot:
            wav_path = await generate_notification_wav("join", member, speaker=888753760)
            if wav_path:
                tts_manager.enqueue(guild.voice_client, guild, build_audio_entry(wav_path))

    if before.channel is not None and after.channel is None:
        vc = guild.voice_client
        if vc is None or member.bot:
            return
        if before.channel != vc.channel:
            return

        non_bot_members = [m for m in before.channel.members if not m.bot]
        if not non_bot_members:
            async with guild_lock(guild.id):
                try:
                    await vc.disconnect()
                except Exception:
                    await vc.disconnect(force=True)
            return

        wav_path = await generate_notification_wav("leave", member, speaker=888753760)
        if wav_path:
            tts_manager.enqueue(vc, guild, build_audio_entry(wav_path))

    vc = guild.voice_client
    if vc is not None:
        non_bot_members = [m for m in vc.channel.members if not m.bot]
        if not non_bot_members:
            async with guild_lock(guild.id):
                try:
                    await vc.disconnect()
                except Exception:
                    await vc.disconnect(force=True)

# ===============================
# クライアント起動
# ===============================
client.run(discord_access_token)
