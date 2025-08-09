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
import subprocess
import wave
import contextlib
import tempfile
from src import checker
from src import config as cfg_module
from src import guild_tts_manager as tts_manager_module

# -------------------------------
# 基本ディレクトリとグローバル設定
# -------------------------------
SAVED_WAV_DIR = 'saved_wav'
TEMP_WAV_DIR = os.path.join('temp', 'wav')
os.makedirs(SAVED_WAV_DIR, exist_ok=True)
os.makedirs(TEMP_WAV_DIR, exist_ok=True)

# 事前定義された音声（キャラクター）ID一覧
available_voice_ids = [
    {"name": "Anneli", "id": 888753760}
]

# ユーザー別の音声マッピング（永続化ファイル）
USER_VOICE_MAPPING_FILE = "voice_mapping.yaml"
user_voice_mapping = {}

def save_voice_mapping():
    """ユーザー音声マッピングを YAML に保存"""
    with open(USER_VOICE_MAPPING_FILE, "w") as f:
        yaml.dump(user_voice_mapping, f)

def load_voice_mapping():
    """YAML からユーザー音声マッピングを読み込み"""
    global user_voice_mapping
    if os.path.exists(USER_VOICE_MAPPING_FILE):
        with open(USER_VOICE_MAPPING_FILE, "r") as f:
            user_voice_mapping = yaml.load(f, Loader=yaml.FullLoader) or {}

load_voice_mapping()

def get_random_voice_id():
    """利用可能な音声IDをランダム取得"""
    return random.choice(available_voice_ids)['id']

def get_voice_for_user(user_id: int, display_name: str) -> int:
    """
    ユーザーの音声IDを取得。
    未登録であればランダム割当ての上、マッピングを保存。
    """
    if user_id in user_voice_mapping:
        user_data = user_voice_mapping[user_id]
        if isinstance(user_data, dict):
            return user_data.get('voice_id', 888753760)
        else:
            # 古い形式の互換対応：dict 形式へ変換して保存
            user_voice_mapping[user_id] = {'voice_id': user_data, 'display_name': display_name}
            save_voice_mapping()
            return user_data
    else:
        voice_id = get_random_voice_id()
        user_voice_mapping[user_id] = {'voice_id': voice_id, 'display_name': display_name}
        save_voice_mapping()
        return voice_id

# -------------------------------
# Discord 用 WAV フォーマット検査/保証
# -------------------------------
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

def convert_to_discord_format(input_path: str) -> str:
    """
    WAV を Discord 想定形式（48kHz / ステレオ / 16bit）へ変換。
    直接上書きせず一時ファイルに出力してから置換（原子性確保）。
    """
    dir_ = os.path.dirname(input_path) or "."
    fd, tmp_out = tempfile.mkstemp(suffix=".wav", dir=dir_)
    os.close(fd)

    cmd = [
        'ffmpeg', '-y',
        '-i', input_path,
        '-ar', '48000',
        '-ac', '2',
        '-sample_fmt', 's16',
        tmp_out
    ]
    print(f"[TTS][ffmpeg] {os.path.basename(input_path)} -> temp")
    proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if proc.returncode != 0:
        print(proc.stderr.decode(errors='ignore'))
        raise RuntimeError("[TTS] ffmpeg 変換に失敗しました")

    # 変換後のフォーマット確認
    sr, ch, sw, _ = probe_wav(tmp_out)
    assert (sr == 48000 and ch == 2 and sw == 2), \
        f"変換後フォーマットが不正: sr={sr}, ch={ch}, sw={sw*8}bit"

    shutil.move(tmp_out, input_path)
    return input_path

def ensure_discord_wav(path: str, verbose: bool = True) -> str:
    """
    48kHz/ステレオ/16bit でない場合、ffmpeg で上書き変換して整える（ログ付き）
    """
    try:
        sr, ch, sw, nf = probe_wav(path)
        if verbose:
            print(f"[TTS][probe] {path} sr={sr} ch={ch} sw={sw*8}bit frames={nf}")
    except Exception as e:
        print(f"[TTS][probe] {path} を標準WAVとして読み込めません（{e}）→ 変換を試みます")
        sr, ch, sw = None, None, None

    if not is_discord_wav(sr or 0, ch or 0, sw or 0):
        print(f"[TTS][convert] → 48kHz/ステレオ/16bit に変換: {path}")
        convert_to_discord_format(path)
        # 変換後に再確認
        try:
            nsr, nch, nsw, nnf = probe_wav(path)
            print(f"[TTS][probe-after] {path} sr={nsr} ch={nch} sw={nsw*8}bit frames={nnf}")
        except Exception as e:
            print(f"[TTS][probe-after] 失敗: {e}")
    return path

# -------------------------------
# PCM 直接再生用 AudioSource
# -------------------------------
# Discord は 20ms 単位の PCM 入力を想定（48kHz / ステレオ / 16bit）
# 20ms あたりのサンプル数: 48000 * 0.02 = 960
FRAME_SAMPLES = 960
CHANNELS = 2
BYTES_PER_SAMPLE = 2
FRAME_BYTES = FRAME_SAMPLES * CHANNELS * BYTES_PER_SAMPLE  # 960*2*2=3840 bytes

class WavPCMSource(discord.AudioSource):
    """48kHz/ステレオ/16bit PCM の WAV を直接供給する AudioSource（ffmpeg 非依存）"""
    def __init__(self, wav_path: str):
        self.path = wav_path
        self._wav = wave.open(wav_path, 'rb')
        # フォーマット厳密チェック
        assert self._wav.getframerate() == 48000, "WAV must be 48kHz"
        assert self._wav.getsampwidth() == 2,     "WAV must be 16-bit (s16)"
        assert self._wav.getnchannels() == 2,     "WAV must be stereo (2ch)"
        self._padded_last = False  # 終端の不完全フレームを一度だけ無音でパディング済みか

    def read(self) -> bytes:
        """
        20ms（960フレーム）単位で PCM を返す。
        最終チャンクが 20ms 未満なら無音で埋めて一度だけ返し、その次に空を返す。
        """
        if self._padded_last:
            return b''

        data = self._wav.readframes(FRAME_SAMPLES)
        if not data:
            # 既に終端
            return b''

        if len(data) < FRAME_BYTES:
            # 最後のフレーム不足分を無音で埋め、一回だけ返す
            data += b'\x00' * (FRAME_BYTES - len(data))
            self._padded_last = True

        return data

    def is_opus(self) -> bool:
        """PCM を返すため False"""
        return False

    def cleanup(self):
        """再生終了コールバックで呼ばれるクリーンアップ"""
        try:
            self._wav.close()
        except:
            pass

def build_audio_entry(wav_path: str, saved_dir: str = SAVED_WAV_DIR, volume: float = 1.0):
    """
    再生キュー投入用のエントリ(dict)を作成
      1) 生成時点で Discord 形式に整えている前提（ここでは再チェックしない）
      2) PCM 直接再生を試行（失敗時のみ FFmpeg にフォールバック）
    """
    try:
        # PCM 直接再生を優先（ここでは形式チェックしない）
        try:
            source = WavPCMSource(wav_path)
            source = discord.PCMVolumeTransformer(source, volume=volume)
            used = "PCM"
            print(f"[TTS][use] PCM 直接再生: {wav_path}")
        except Exception as e:
            # 想定外の不整合・破損時は ffmpeg 再生に切り替え（変換はしない）
            print(f"[TTS][fallback] PCM 失敗、FFmpeg に切り替え: {e}")
            source = discord.PCMVolumeTransformer(discord.FFmpegPCMAudio(wav_path), volume=volume)
            used = "FFMPEG"

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

# -------------------------------
# TTS 生成処理
# -------------------------------
async def generate_wav_from_server(text: str, speaker: int, filepath: str, host: str, port: int) -> str:
    """
    指定の TTS サーバーへ音声生成を要求し、生成直後に Discord 想定形式へ整えたパスを返す
    """
    try:
        params = {
            'text': text,
            'speaker': speaker,
            "enable_interrogative_upspeak": "true"
        }
        async with aiohttp.ClientSession() as session:
            # ステップ1：音声クエリ生成
            async with session.post(f'http://{host}:{port}/audio_query', params=params) as resp_query:
                query_data = await resp_query.json()
            headers = {'Content-Type': 'application/json'}
            # ステップ2：音声合成
            async with session.post(
                f'http://{host}:{port}/synthesis',
                headers=headers,
                params=params,
                json=query_data
            ) as resp_synth:
                audio_data = await resp_synth.read()

        # 生成された WAV を保存
        with open(filepath, "wb") as f:
            f.write(audio_data)

        # 生成直後に形式を保証
        ensure_discord_wav(filepath, verbose=True)
        return filepath

    except Exception as e:
        print(f"Error generating wav from {host}:{port} - {e}")
        return None

async def generate_wav(text: str, speaker: int = 888753760, file_dir: str = TEMP_WAV_DIR) -> str:
    """
    複数の TTS サーバーを競わせ、最初に成功した音声ファイルのパスを返す
    """
    servers = [
        {"host": "localhost", "port": 10101},
        {"host": "192.168.0.246", "port": 10101}
    ]
    tasks = []
    for server in servers:
        msg_uuid = str(uuid.uuid4())
        filepath = os.path.join(file_dir, f"{msg_uuid}.wav")
        tasks.append(asyncio.create_task(
            generate_wav_from_server(text, speaker, filepath, server["host"], server["port"])
        ))
    done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    valid_result = None
    for task in done:
        try:
            result = task.result()
            if result and os.path.exists(result):
                valid_result = result
                break
            else:
                print(f"Invalid result from task: {result}")
        except Exception as e:
            print(f"Task error: {e}")
    if valid_result:
        for p in pending:
            p.cancel()
        return valid_result
    # 最初のタスクが無効な場合、残りも順次確認
    for task in pending:
        try:
            result = await task
            if result and os.path.exists(result):
                return result
        except Exception as e:
            print(f"Pending task error: {e}")
    return None

async def generate_notification_wav(action: str, user, speaker: int = 888753760) -> str:
    """
    入室/退室の通知音声を生成し、キャッシュしたファイルパスを返す
      action: "join" または "leave"
    """
    guild_id = user.guild.id
    user_id = user.id
    display_name = user.display_name
    filename = f"{action}_{guild_id}_{user_id}.wav"
    filepath = os.path.join(SAVED_WAV_DIR, filename)
    # 既にキャッシュがあればそれを使用
    if os.path.exists(filepath):
        return filepath
    text = f"{display_name} さんが{'入室' if action == 'join' else '退室'}しました。"
    temp_wav = await generate_wav(text, speaker)
    if temp_wav and os.path.exists(temp_wav):
        shutil.move(temp_wav, filepath)
        return filepath
    return None

# -------------------------------
# Discord Bot 設定
# -------------------------------
config_obj = cfg_module.Config()
tts_manager = tts_manager_module.guild_tts_manager()

discord_access_token = config_obj.discord_access_token
discord_application_id = config_obj.discord_application_id

intents = discord.Intents.all()
client = discord.Client(intents=intents)
tree = app_commands.CommandTree(client)

@client.event
async def on_ready():
    print("Bot started!")
    print(f"https://discord.com/api/oauth2/authorize?client_id={discord_application_id}&permissions=3148864&scope=bot%20applications.commands")
    await tree.sync()

@tree.command(name="vjoin", description="ボイスチャンネルにボットを参加させます。")
async def join_command(interaction: discord.Interaction):
    if interaction.user.voice is None:
        await interaction.response.send_message("先にボイスチャンネルへ参加してください。", ephemeral=True)
        return
    if interaction.guild.voice_client is not None and interaction.guild.voice_client.is_connected():
        await interaction.response.send_message("ボットは既にボイスチャンネルに接続しています。", ephemeral=True)
    else:
        await interaction.user.voice.channel.connect()
        await interaction.response.send_message("ボイスチャンネルに接続しました。")
        wav_path = os.path.abspath(os.path.join(SAVED_WAV_DIR, 'bot_join.wav'))
        print(f"[TTS] enqueue → {wav_path}")
        tts_manager.enqueue(interaction.guild.voice_client, interaction.guild, build_audio_entry(wav_path))

@tree.command(name="vleave", description="ボイスチャンネルからボットを切断します。")
async def leave_command(interaction: discord.Interaction):
    vc = interaction.guild.voice_client
    if vc is not None and vc.is_connected():
        if interaction.channel.id == vc.channel.id:
            await vc.disconnect()
            await interaction.response.send_message("切断しました。")
        else:
            await interaction.response.send_message("ボットのいるチャンネルでコマンドを実行してください。", ephemeral=True)
    else:
        await interaction.response.send_message("ボットはボイスチャンネルに接続していません。", ephemeral=True)

@tree.command(name="list_voices", description="利用可能な声線を一覧表示します。")
async def list_voices_command(interaction: discord.Interaction):
    user_id = interaction.user.id
    user_data = user_voice_mapping.get(user_id, {})
    current_voice_id = user_data.get('voice_id')
    voice_lines = "キャラクター名\tID\n"
    for voice in available_voice_ids:
        if voice['id'] == current_voice_id:
            voice_lines += f"{voice['name']}\t{voice['id']} （現在の設定）\n"
        else:
            voice_lines += f"{voice['name']}\t{voice['id']}\n"
    await interaction.response.send_message(content=voice_lines, ephemeral=True)

@tree.command(name="set_voice", description="自分の声線を設定します。")
async def set_voice_command(interaction: discord.Interaction, voice_id: int):
    user_id = interaction.user.id
    current_display_name = interaction.user.display_name
    valid_voice_ids = [voice['id'] for voice in available_voice_ids]
    if voice_id not in valid_voice_ids:
        await interaction.response.send_message("無効な声線 ID です。/list_voices で確認してください。", ephemeral=True)
    else:
        user_voice_mapping[user_id] = {'voice_id': voice_id, 'display_name': current_display_name}
        save_voice_mapping()
        voice_name = next((voice['name'] for voice in available_voice_ids if voice['id'] == voice_id), "不明な声線")
        await interaction.response.send_message(f"あなたの声線を (ID: {voice_id} {voice_name}) に設定しました。", ephemeral=True)

# -------------------------------
# メッセージイベント & 再生キュー投入
# -------------------------------
@client.event
async def on_message(message: discord.Message):
    # Bot のメッセージや DM は無視
    if message.author.bot or message.guild is None:
        return

    vc = message.guild.voice_client
    if vc is None:
        return

    wav_path = None

    # Bot が接続しているボイスチャンネルと紐づくテキストのみ処理
    if message.channel.id != vc.channel.id:
        return

    user_id = message.author.id
    # ユーザーの声線 ID を取得（未登録ならランダム付与）
    speaker_id = get_voice_for_user(user_id, message.author.display_name)

    # 元メッセージの整形
    original_content = message.content.strip()

    # カスタム絵文字を除去
    custom_emoji_pattern = re.compile(r'<a?:(\w+):(\d+)>')
    content = re.sub(custom_emoji_pattern, '', original_content)
    content = emoji.replace_emoji(content, replace="")

    # "neko!" で始まるメッセージ（Music Bot 用）は無視
    if content.lower().startswith("neko!"):
        return

    # メンションを表示名に変換
    for mentioned in message.mentions:
        content = content.replace(f"<@{mentioned.id}>", f"アットマーク {mentioned.display_name}")
    for role in message.role_mentions:
        content = content.replace(f"<@&{role.id}>", f"アットマーク {role.name}")

    # 添付ファイルがある場合は専用音声を使用
    if message.attachments:
        wav_path = os.path.abspath(os.path.join(SAVED_WAV_DIR, 'attachment.wav'))
    else:
        # URL の正規表現
        url_pattern = re.compile(r'https?://[^\s]+')

        # メッセージが URL のみか判定
        if re.fullmatch(url_pattern, original_content):
            # 完全に URL のみなら事前用意の url.wav を使用
            wav_path = os.path.abspath(os.path.join(SAVED_WAV_DIR, 'url.wav'))
        else:
            # テキスト中の URL を固定語「URL」に置換
            content = url_pattern.sub("URL", content)
            # 長文は上限で切り詰め
            if len(content) > config_obj.max_text_length:
                content = content[:config_obj.max_text_length] + "以下省略"
            # 無視対象でなければ TTS 生成
            if content.strip() and not checker.ignore_check(content):
                wav_path = await generate_wav(content, speaker_id)

    # 生成 or 指定された音声をキューへ投入
    if wav_path is not None and os.path.exists(wav_path):
        tts_manager.enqueue(vc, message.guild, build_audio_entry(wav_path))

@client.event
async def on_voice_state_update(member: discord.Member, before: discord.VoiceState, after: discord.VoiceState):
    guild = member.guild
    vc = guild.voice_client

    # 同一ギルド内でのチャンネル移動
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
                tts_manager.enqueue(guild.voice_client, after.channel, build_audio_entry(wav_path))

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
            tts_manager.enqueue(vc, before.channel, build_audio_entry(wav_path))

    # 最終チェック：Bot のいるチャンネルに非Bot がいなければ切断
    vc = guild.voice_client
    if vc is not None:
        non_bot_members = [m for m in vc.channel.members if not m.bot]
        if not non_bot_members:
            await vc.disconnect()

client.run(discord_access_token)
