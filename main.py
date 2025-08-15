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
SAVED_WAV_DIR = 'saved_wav'                 # 固定効果音・通知音などを置くフォルダ
TEMP_WAV_DIR = os.path.join('temp', 'wav')  # TTS 一時生成先
os.makedirs(SAVED_WAV_DIR, exist_ok=True)
os.makedirs(TEMP_WAV_DIR, exist_ok=True)

# TTS の各種タイムアウト（秒）
PER_REQUEST_TIMEOUT = 10.0   # 各サーバーでの1件の生成上限
FIRST_REPLY_TIMEOUT = 10.0   # 複数サーバー競争で最初の結果を待つ上限

# よく使う正規表現は事前にコンパイル（毎回の compile コスト削減）
CUSTOM_EMOJI_RE = re.compile(r'<a?:(\w+):(\d+)>')
URL_RE = re.compile(r'https?://[^\s]+')

# ===============================
# 音声（キャラクター）ID 定義
# ===============================
available_voice_ids = [
    {"name": "Anneli", "id": 888753760}
]

# 高頻度参照のため起動時に前計算
VOICE_ID_SET = {v["id"] for v in available_voice_ids}                 # 有効ID集合
VOICE_ID_NAME = {v["id"]: v["name"] for v in available_voice_ids}     # id -> 名前

# ===============================
# ユーザー別の音声マッピング（YAML 永続化）
# ===============================
USER_VOICE_MAPPING_FILE = "voice_mapping.yaml"
user_voice_mapping: Dict[int, dict] = {}

def load_voice_mapping():
    """YAML からユーザー音声マッピングを読み込む（なければ空）"""
    global user_voice_mapping
    if os.path.exists(USER_VOICE_MAPPING_FILE):
        with open(USER_VOICE_MAPPING_FILE, "r") as f:
            user_voice_mapping = yaml.safe_load(f) or {}
    else:
        user_voice_mapping = {}

# 保存のデバウンス制御（短時間の連続更新で I/O を抑制）
_save_pending = False
def save_voice_mapping_debounced(delay: float = 0.8):
    """
    連続更新時のディスク I/O を抑えるため、保存を少し遅延させてまとめる。
    """
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

    # ランタイム中のイベントループで遅延タスク実行
    try:
        asyncio.get_running_loop().create_task(_later())
    except RuntimeError:
        # ループ未起動（起動直後など）の場合は即時保存にフォールバック
        with open(USER_VOICE_MAPPING_FILE, "w") as f:
            yaml.safe_dump(user_voice_mapping, f, allow_unicode=True)

load_voice_mapping()

def get_random_voice_id() -> int:
    """利用可能な音声IDをランダム取得"""
    return random.choice(available_voice_ids)['id']

def get_voice_for_user(user_id: int, display_name: str) -> int:
    """
    ユーザーの音声IDを取得。
    未登録ならランダム割当てのうえ、YAML 保存（デバウンス）。
    """
    if user_id in user_voice_mapping:
        user_data = user_voice_mapping[user_id]
        if isinstance(user_data, dict):
            return user_data.get('voice_id', 888753760)
        # 旧形式（int）互換：dict に移行して保存
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
DISCORD_SR = 48000  # サンプリングレート
DISCORD_CH = 2      # チャンネル数（ステレオ）
DISCORD_SW = 2      # サンプル幅（バイト）= 16-bit

def probe_wav(path: str):
    """WAV を開いて (sr, ch, sampwidth_bytes, nframes) を返す（非WAVは例外）"""
    with contextlib.closing(wave.open(path, 'rb')) as w:
        return (w.getframerate(), w.getnchannels(), w.getsampwidth(), w.getnframes())

def is_discord_wav(sr: int, ch: int, sw: int) -> bool:
    """Discord 再生向けの想定形式かどうかを判定"""
    return (sr == DISCORD_SR and ch == DISCORD_CH and sw == DISCORD_SW)

# ===============================
# PCM 直接再生用 AudioSource（FFmpeg 不使用）
# ===============================
# Discord への入力は 20ms 単位の PCM（48kHz / ステレオ / 16bit）を想定
FRAME_SAMPLES = 960                     # 48kHz * 0.02s = 960
CHANNELS = 2
BYTES_PER_SAMPLE = 2
FRAME_BYTES = FRAME_SAMPLES * CHANNELS * BYTES_PER_SAMPLE  # 960*2*2=3840 bytes

class WavPCMSource(discord.AudioSource):
    """48kHz / ステレオ / 16bit の WAV をそのまま供給する AudioSource"""
    def __init__(self, wav_path: str):
        self.path = wav_path
        self._wav = wave.open(wav_path, 'rb')

        # 期待フォーマットの明示チェック（例外で安定動作）
        if self._wav.getframerate() != 48000:
            raise ValueError("WAV must be 48kHz")
        if self._wav.getsampwidth() != 2:
            raise ValueError("WAV must be 16-bit (s16)")
        if self._wav.getnchannels() != 2:
            raise ValueError("WAV must be stereo (2ch)")

        self._padded_last = False  # 終端の不完全フレームを一度だけ無音でパディングしたか

    def read(self) -> bytes:
        """
        20ms（960フレーム）単位で PCM を返す。
        終端が 20ms 未満なら不足分を無音で埋めて一度だけ返し、次回は空を返す。
        """
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
        """PCM を返すため False"""
        return False

    def cleanup(self):
        """再生終了時に呼ばれるクリーンアップ"""
        try:
            self._wav.close()
        except:
            pass

def build_audio_entry(wav_path: str, saved_dir: str = SAVED_WAV_DIR, volume: float = 1.0):
    """
    再生キュー投入用のエントリ dict を作成。
    ここで WavPCMSource の生成に失敗（フォーマット不一致等）した場合は例外。
    """
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
    """
    共有の aiohttp.ClientSession を取得。
    未生成または閉じている場合は再生成して返す。
    """
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
# TTS 生成（FFmpeg 非依存）
#  - キャッシュなし
#  - in-flight 統合なし
#  - 並列競争は as_completed（最初に成功したサーバーを採用）
# ===============================
async def generate_wav_from_server(text: str, speaker: int, filepath: str, host: str, port: int) -> Optional[str]:
    """
    VOICEVOX 互換 API を利用して WAV を生成する。
      1) /audio_query で合成パラメータを取得
      2) /synthesis で 48kHz / ステレオ の WAV を受け取る
    受信後はフォーマットを即検査し、想定外なら None を返す。
    """
    session = await get_http_session()

    async def _do_one():
        params = {'text': text, 'speaker': speaker, "enable_interrogative_upspeak": "true"}
        req_to = aiohttp.ClientTimeout(total=PER_REQUEST_TIMEOUT)

        # Step 1: audio_query
        async with session.post(
            f'http://{host}:{port}/audio_query',
            params=params,
            timeout=req_to
        ) as resp_query:
            resp_query.raise_for_status()
            query_data = await resp_query.json()

        # Discord 互換出力を要求
        query_data["outputSamplingRate"] = 48000
        query_data["outputStereo"] = True

        # Step 2: synthesis
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

        # ファイル保存
        with open(filepath, "wb") as f:
            f.write(audio_data)

        # 即フォーマット検査
        sr, ch, sw, _ = probe_wav(filepath)
        if not is_discord_wav(sr, ch, sw):
            raise ValueError(f"不正なWAV形式: sr={sr}, ch={ch}, sw={sw*8}bit（48kHz/2ch/16bit が必須）")

        return filepath

    try:
        return await asyncio.wait_for(_do_one(), timeout=PER_REQUEST_TIMEOUT)
    except Exception as e:
        # 例外時は一時ファイルを削除して None
        try:
            if os.path.exists(filepath):
                os.remove(filepath)
        except:
            pass
        print(f"Error generating wav from {host}:{port} - {e}")
        return None

async def generate_wav(text: str, speaker: int = 888753760, file_dir: str = TEMP_WAV_DIR) -> Optional[str]:
    """
    複数 TTS サーバーに並列で投げ、最初に成功した結果を採用。
    """
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
        # 先着成功のタスクを採用
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
        # 勝者以外のタスクをキャンセル＆残骸削除
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
_last_notice_name: Dict[tuple, str] = {}  # (guild_id, user_id, action) -> display_name

async def generate_notification_wav(action: str, user, speaker: int = 888753760) -> Optional[str]:
    """
    入室/退室の通知音声を生成し、キャッシュしたファイルパスを返す。
      action: "join" または "leave"
    """
    guild_id = user.guild.id
    user_id = user.id
    display_name = user.display_name
    key = (guild_id, user_id, action)

    filename = f"{action}_{guild_id}_{user_id}.wav"
    filepath = os.path.join(SAVED_WAV_DIR, filename)

    # 既存ファイルかつ表示名が変わっていなければ再利用
    if os.path.exists(filepath) and _last_notice_name.get(key) == display_name:
        return filepath

    text = f"{display_name} さんが{'入室' if action == 'join' else '退室'}しました。"
    temp_wav = await generate_wav(text, speaker)
    if temp_wav and os.path.exists(temp_wav):
        shutil.move(temp_wav, filepath)
        _last_notice_name[key] = display_name
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

async def restart_voice(guild: discord.Guild):
    """音声接続を強制的に再確立（幽霊状態も考慮）"""
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
    """幽霊状態の強制解除（server 側の voice state をリセット）"""
    try:
        await guild.change_voice_state(channel=None, self_mute=False, self_deaf=False)
        print(f"[VOICE] force_drop_voice done guild={guild.id}")
    except Exception as e:
        print(f"[VOICE] force_drop_voice error guild={guild.id}: {e}")

async def safe_connect(channel: discord.VoiceChannel, timeout: float = 8.0, retries: int = 2):
    """安全な connect：タイムアウト監視＋リトライ（幽霊対策）"""
    for i in range(retries + 1):
        try:
            return await asyncio.wait_for(channel.connect(), timeout=timeout)
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
                await safe_connect(user_ch)     # move は内部的に再接続扱い
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

        if vc and vc.is_connected():
            await vc.disconnect()
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
    wav_path: Optional[str] = await generate_wav(content, speaker_id)

    # 11) 成功したら再生キューへ投入（TEMP 生成物は再生後に自動削除）
    if wav_path and os.path.exists(wav_path):
        tts_manager.enqueue(vc, message.guild, build_audio_entry(wav_path))

# ===============================
# ボイス状態イベント
# ===============================
@client.event
async def on_voice_state_update(member: discord.Member, before: discord.VoiceState, after: discord.VoiceState):
    guild = member.guild
    vc = guild.voice_client

    # 同一ギルド内のチャンネル移動
    if before.channel is not None and after.channel is not None and before.channel != after.channel:
        if vc is not None:
            # 他チャンネル → Bot のチャンネルへ移動：入室通知
            if after.channel == vc.channel:
                if not member.bot:
                    wav_path = await generate_notification_wav("join", member, speaker=888753760)
                    if wav_path:
                        tts_manager.enqueue(vc, guild, build_audio_entry(wav_path))
            # Bot のチャンネル → 他チャンネル：退室通知
            elif before.channel == vc.channel:
                if not member.bot:
                    wav_path = await generate_notification_wav("leave", member, speaker=888753760)
                    if wav_path:
                        tts_manager.enqueue(vc, guild, build_audio_entry(wav_path))
        else:
            # 未接続：移動先へ接続して起動音を再生
            await after.channel.connect()
            vc = guild.voice_client
            bot_join_path = os.path.abspath(os.path.join(SAVED_WAV_DIR, 'bot_join.wav'))
            tts_manager.enqueue(vc, guild, build_audio_entry(bot_join_path))

    # 新規参加（before 無し → after あり）
    if before.channel is None and after.channel is not None:
        if guild.voice_client is not None:
            if after.channel != guild.voice_client.channel:
                return
        else:
            # 未接続：参加先へ接続して起動音を再生
            await after.channel.connect()
            vc = guild.voice_client
            bot_join_path = os.path.abspath(os.path.join(SAVED_WAV_DIR, 'bot_join.wav'))
            tts_manager.enqueue(vc, guild, build_audio_entry(bot_join_path))
            return

        # Bot 以外のユーザーは入室通知を再生
        if not member.bot:
            wav_path = await generate_notification_wav("join", member, speaker=888753760)
            if wav_path:
                # 第二引数に guild を渡す（queue キー不一致の回避）
                tts_manager.enqueue(guild.voice_client, guild, build_audio_entry(wav_path))

    # 退出（before あり → after 無し）
    if before.channel is not None and after.channel is None:
        if vc is None or member.bot:
            return
        if before.channel != vc.channel:
            return

        # 非Bot メンバーが残っていなければ切断
        non_bot_members = [m for m in before.channel.members if not m.bot]
        if not non_bot_members:
            await vc.disconnect()
            return

        # 残っている場合は退室通知を再生
        wav_path = await generate_notification_wav("leave", member, speaker=888753760)
        if wav_path:
            tts_manager.enqueue(vc, guild, build_audio_entry(wav_path))

    # 最終チェック：Bot のいるチャンネルに非Bot がいなければ切断
    vc = guild.voice_client
    if vc is not None:
        non_bot_members = [m for m in vc.channel.members if not m.bot]
        if not non_bot_members:
            await vc.disconnect()

# ===============================
# クライアント起動
# ===============================
client.run(discord_access_token)
