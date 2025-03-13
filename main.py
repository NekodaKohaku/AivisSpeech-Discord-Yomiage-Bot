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

# 事前定義された音声リスト
available_voice_ids = [
    {"name": "Anneli", "id": 888753760},
    {"name": "decoprokun", "id": 604172608},
    {"name": "fumifumi", "id": 606865152},
    {"name": "hinakoyuhara", "id": 2058221184},
    {"name": "peach", "id": 933744512},
    {"name": "white", "id": 706073888},
    {"name": "yukyu", "id": 1099751712},
    {"name": "にせ", "id": 1937616896},
    {"name": "まい", "id": 1431611904},
    {"name": "ろてじん（長老ボイス）", "id": 391794336},
    {"name": "亜空マオ", "id": 532977856},
    {"name": "凛音エル", "id": 1388823424},
    {"name": "天深シノ", "id": 1063997408},
    {"name": "宗周定昌", "id": 1143949696},
    {"name": "様子ヶ丘シイナ", "id": 1130341985},
    {"name": "立神ケイ", "id": 87094656},
    {"name": "観測症", "id": 1275216064},
    {"name": "Furina", "id": 134921440},
    {"name": "Lunlun", "id": 788751232},
    {"name": "Mita", "id": 1292986496},
    {"name": "花火", "id": 591215776},
    {"name": "Nahida", "id": 1206699648},
    {"name": "KikotoMahiro", "id": 1430982625},
    {"name": "Paimon", "id": 1031189312},
    {"name": "ユニ", "id": 1105189120}
]

# ユーザー音声マッピングファイル
USER_VOICE_MAPPING_FILE = "voice_mapping.yaml"
user_voice_mapping = {}

def save_voice_mapping():
    """ユーザー音声マッピングを YAML ファイルに保存する"""
    with open(USER_VOICE_MAPPING_FILE, "w") as f:
        yaml.dump(user_voice_mapping, f)

def load_voice_mapping():
    """YAML ファイルからユーザー音声マッピングを読み込む"""
    global user_voice_mapping
    if os.path.exists(USER_VOICE_MAPPING_FILE):
        with open(USER_VOICE_MAPPING_FILE, "r") as f:
            user_voice_mapping = yaml.load(f, Loader=yaml.FullLoader) or {}

load_voice_mapping()

def get_random_voice_id():
    """ランダムに利用可能な音声IDを返す"""
    return random.choice(available_voice_ids)['id']

def get_voice_for_user(user_id: int, display_name: str) -> int:
    """
    ユーザーの音声IDを取得する。
    存在しない場合はランダムに割り当て、マッピングを保存する。
    """
    if user_id in user_voice_mapping:
        user_data = user_voice_mapping[user_id]
        if isinstance(user_data, dict):
            return user_data.get('voice_id', 888753760)
        else:
            # データ形式が正しくない場合、マッピング構造を更新する
            user_voice_mapping[user_id] = {'voice_id': user_data, 'display_name': display_name}
            save_voice_mapping()
            return user_data
    else:
        voice_id = get_random_voice_id()
        user_voice_mapping[user_id] = {'voice_id': voice_id, 'display_name': display_name}
        save_voice_mapping()
        return voice_id

# -------------------------------
# TTS生成関数
# -------------------------------
async def generate_wav_from_server(text: str, speaker: int, filepath: str, host: str, port: int) -> str:
    """
    指定したTTSサーバーに音声生成をリクエストし、ファイルに保存する
    """
    try:
        params = {
            'text': text,
            'speaker': speaker,
            "enable_interrogative_upspeak": "true"
        }
        async with aiohttp.ClientSession() as session:
            # ステップ1：音声クエリ生成リクエスト
            async with session.post(f'http://{host}:{port}/audio_query', params=params) as resp_query:
                query_data = await resp_query.json()
            headers = {'Content-Type': 'application/json'}
            # ステップ2：音声合成リクエスト
            async with session.post(
                f'http://{host}:{port}/synthesis',
                headers=headers,
                params=params,
                json=query_data
            ) as resp_synth:
                audio_data = await resp_synth.read()
        with open(filepath, "wb") as f:
            f.write(audio_data)
        return filepath
    except Exception as e:
        print(f"Error generating wav from {host}:{port} - {e}")
        return None

async def generate_wav(text: str, speaker: int = 888753760, file_dir: str = TEMP_WAV_DIR) -> str:
    """
    複数のTTSサーバーを使用して、最も早く応答した音声ファイルのパスを返す
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
    # 最初のタスクが無効な場合、他のタスクを確認する
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
    ユーザーの入室または退室に応じた通知音声を生成する
    action: "join" または "leave"
    """
    guild_id = user.guild.id
    user_id = user.id
    display_name = user.display_name
    filename = f"{action}_{guild_id}_{user_id}.wav"
    filepath = os.path.join(SAVED_WAV_DIR, filename)
    # キャッシュされた音声ファイルが存在する場合は、そのまま返す
    if os.path.exists(filepath):
        return filepath
    text = f"{display_name} さんが{'入室' if action == 'join' else '退室'}しました。"
    temp_wav = await generate_wav(text, speaker)
    if temp_wav and os.path.exists(temp_wav):
        shutil.move(temp_wav, filepath)
        return filepath
    return None

# -------------------------------
# Discord Bot設定
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

@tree.command(name="vjoin", description="ボイスチャットにボットを追加。")
async def join_command(interaction: discord.Interaction):
    if interaction.user.voice is None:
        await interaction.response.send_message("先に、ボイスチャンネルに接続してください。", ephemeral=True)
        return
    if interaction.guild.voice_client is not None and interaction.guild.voice_client.is_connected():
        await interaction.response.send_message("ボットは既にボイスチャンネルに接続しています。", ephemeral=True)
    else:
        await interaction.user.voice.channel.connect()
        await interaction.response.send_message("ボイスチャンネルに接続しました。")
        wav_path = os.path.abspath(os.path.join(SAVED_WAV_DIR, 'bot_join.wav'))
        tts_manager.enqueue(interaction.guild.voice_client, interaction.guild, discord.FFmpegPCMAudio(wav_path))

@tree.command(name="vleave", description="ボイスチャットから切断します。")
async def leave_command(interaction: discord.Interaction):
    vc = interaction.guild.voice_client
    if vc is not None and vc.is_connected():
        if interaction.channel.id == vc.channel.id:
            await vc.disconnect()
            await interaction.response.send_message("切断しました。")
        else:
            await interaction.response.send_message("ボットが存在するチャンネルでコマンドを使用してください。", ephemeral=True)
    else:
        await interaction.response.send_message("ボットはボイスチャットに接続していません。", ephemeral=True)

@tree.command(name="list_voices", description="利用可能な声線を一覧表示します")
async def list_voices_command(interaction: discord.Interaction):
    user_id = interaction.user.id
    user_data = user_voice_mapping.get(user_id, {})
    current_voice_id = user_data.get('voice_id')
    voice_lines = "キャラクター名\tID\n"
    for voice in available_voice_ids:
        if voice['id'] == current_voice_id:
            voice_lines += f"{voice['name']}\t{voice['id']} （現在の声線）\n"
        else:
            voice_lines += f"{voice['name']}\t{voice['id']}\n"
    await interaction.response.send_message(content=voice_lines, ephemeral=True)

@tree.command(name="set_voice", description="声線を設定します")
async def set_voice_command(interaction: discord.Interaction, voice_id: int):
    user_id = interaction.user.id
    current_display_name = interaction.user.display_name
    valid_voice_ids = [voice['id'] for voice in available_voice_ids]
    if voice_id not in valid_voice_ids:
        await interaction.response.send_message("無効な声線 ID です。利用可能な声線リストを確認してください。", ephemeral=True)
    else:
        user_voice_mapping[user_id] = {'voice_id': voice_id, 'display_name': current_display_name}
        save_voice_mapping()
        voice_name = next((voice['name'] for voice in available_voice_ids if voice['id'] == voice_id), "不明な声線")
        await interaction.response.send_message(f"あなたの声線は (ID: {voice_id} {voice_name}) に設定されました。", ephemeral=True)

# -------------------------------
# メッセージおよび音声再生イベント
# -------------------------------
@client.event
async def on_message(message: discord.Message):
    # Botのメッセージや、ギルド外のメッセージは無視する
    if message.author.bot or message.guild is None:
        return

    vc = message.guild.voice_client
    if vc is None:
        return

    # Botが接続しているボイスチャンネルに対応するテキストチャンネルのみ処理する
    if message.channel.id != vc.channel.id:
        return

    user_id = message.author.id
    # ユーザーの音声IDを取得する（存在しない場合はランダムに割り当て、マッピングを保存する）
    speaker_id = get_voice_for_user(user_id, message.author.display_name)

    # 元のメッセージ内容を取得し、前後の空白を除去する
    original_content = message.content.strip()

    # カスタム絵文字を削除する
    custom_emoji_pattern = re.compile(r'<a?:(\w+):(\d+)>')
    content = re.sub(custom_emoji_pattern, '', original_content)
    content = emoji.replace_emoji(content, replace="")

    # "neko!" で始まるメッセージは無視する (Music Bot コマンド)
    if content.lower().startswith("neko!"):
        return

    # メンションされたユーザーおよびロールを、表示名に置換する
    for mentioned in message.mentions:
        content = content.replace(f"<@{mentioned.id}>", f"アットマーク {mentioned.display_name}")
    for role in message.role_mentions:
        content = content.replace(f"<@&{role.id}>", f"アットマーク {role.name}")

    # 添付ファイルが存在する場合は、添付ファイル用の音声ファイルを使用する
    if message.attachments:
        wav_path = os.path.abspath(os.path.join(SAVED_WAV_DIR, 'attachment.wav'))
    else:
        # URLの正規表現パターン
        url_pattern = re.compile(r'https?://[^\s]+')

        # メッセージが完全にURLのみで構成されているか確認する
        if re.fullmatch(url_pattern, original_content):
            # 完全にURLのみの場合、あらかじめ保存された url.wav を使用する
            wav_path = os.path.abspath(os.path.join(SAVED_WAV_DIR, 'url.wav'))
        else:
            # テキスト中のURLを"URL"という固定文字列に置換する
            content = url_pattern.sub("URL", content)
            # テキストが最大文字数を超える場合は切り捨てる
            if len(content) > config_obj.max_text_length:
                content = content[:config_obj.max_text_length] + "以下省略"
            # テキストが空でなく、かつ無視すべき内容でなければTTS生成を行う
            if content.strip() and not checker.ignore_check(content):
                wav_path = await generate_wav(content, speaker_id)

    # 音声ファイルが生成または指定され、かつ存在する場合は再生キューに追加する
    if wav_path and os.path.exists(wav_path):
        tts_manager.enqueue(vc, message.guild, discord.FFmpegPCMAudio(wav_path))

@client.event
async def on_voice_state_update(member: discord.Member, before: discord.VoiceState, after: discord.VoiceState):
    guild = member.guild
    vc = guild.voice_client

    # チャンネル移動の処理: ユーザーが1つのボイスチャンネルから別のボイスチャンネルに移動する場合
    if before.channel is not None and after.channel is not None and before.channel != after.channel:
        if vc is not None:
            # ユーザーが他のチャンネルから、ボットがいるチャンネルに移動した場合、入室通知を再生する
            if after.channel == vc.channel:
                if not member.bot:
                    wav_path = await generate_notification_wav("join", member, speaker=888753760)
                    if wav_path:
                        tts_manager.enqueue(vc, guild, discord.FFmpegPCMAudio(wav_path))
            # ユーザーがボットがいるチャンネルから、他のチャンネルに移動した場合、退室通知を再生する
            elif before.channel == vc.channel:
                if not member.bot:
                    wav_path = await generate_notification_wav("leave", member, speaker=888753760)
                    if wav_path:
                        tts_manager.enqueue(vc, guild, discord.FFmpegPCMAudio(wav_path))
        else:
            # ボットが未接続の場合、ユーザーが移動した先のチャンネルにボットを接続し
            await after.channel.connect()
            vc = guild.voice_client
            bot_join_path = os.path.abspath(os.path.join(SAVED_WAV_DIR, 'bot_join.wav'))
            tts_manager.enqueue(vc, guild, discord.FFmpegPCMAudio(bot_join_path))

    # ユーザーがボイスチャンネルに参加した場合（before.channel が None で after.channel が存在する場合）
    if before.channel is None and after.channel is not None:
        # Botが既に接続している場合、参加したチャンネルがBotの所在チャンネルと同じか確認する
        if guild.voice_client is not None:
            if after.channel != guild.voice_client.channel:
                # Botが接続しているチャンネルと異なる場合は、入室通知を再生しない
                return
        else:
            # Botがまだ接続していない場合は、ユーザーが参加したチャンネルにBotを接続する
            await after.channel.connect()
            vc = guild.voice_client
            bot_join_path = os.path.abspath(os.path.join(SAVED_WAV_DIR, 'bot_join.wav'))
            tts_manager.enqueue(vc, guild, discord.FFmpegPCMAudio(bot_join_path))
            return
            
        # Bot自身ではないユーザーの場合、入室通知音声を生成して再生する
        if not member.bot:
            wav_path = await generate_notification_wav("join", member, speaker=888753760)
            if wav_path:
                tts_manager.enqueue(guild.voice_client, after.channel, discord.FFmpegPCMAudio(wav_path))

    # ユーザーがボイスチャンネルから退出した場合（before.channel が存在し after.channel が None の場合）
    if before.channel is not None and after.channel is None:
        # Botが該当チャンネルに接続していない場合や、退出したユーザーがBotの場合は何もしない
        if vc is None or member.bot:
            return
        # 退出したチャンネルがBotの接続しているチャンネルかどうかを確認する
        if before.channel != vc.channel:
            return

        # 退出後、チャンネル内に非Botメンバーが残っているかチェックする
        non_bot_members = [m for m in before.channel.members if not m.bot]
        if not non_bot_members:
            # 非Botメンバーがいない場合は、離室通知を再生せずにBotを切断する
            await vc.disconnect()
            return

        # 非Botメンバーが残っている場合は、離室通知音声を生成して再生する
        wav_path = await generate_notification_wav("leave", member, speaker=888753760)
        if wav_path:
            tts_manager.enqueue(vc, before.channel, discord.FFmpegPCMAudio(wav_path))
    
    # 最終チェック：Botが接続しているボイスチャンネルに、Bot自身以外のメンバーが全くいなければ、Botを切断する
    vc = guild.voice_client  # 最新の接続状態を取得
    if vc is not None:
        non_bot_members = [m for m in vc.channel.members if not m.bot]
        if not non_bot_members:
            await vc.disconnect()





client.run(discord_access_token)
