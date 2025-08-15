from collections import defaultdict, deque
from typing import Deque, Dict, Optional, Union
import os
import discord

# ギルドごとの再生キュー
queue_dict: Dict[int, Deque] = defaultdict(deque)

def _get_queue(guild_id: int) -> Deque:
    """ギルドIDに対応する再生キューを取得（なければ作成）"""
    return queue_dict[guild_id]

def _get_loop(vc: discord.VoiceClient):
    """VoiceClient の内部ループ（thread-safe 呼び出し用）"""
    return getattr(getattr(vc, "_state", None), "loop", None)

class guild_tts_manager:
    def __init__(self):
        pass

    def enqueue(self, voice_client: Optional[discord.VoiceClient], guild: Optional[discord.Guild], audio_entry: Union[discord.AudioSource, dict]):
        """
        エントリ（AudioSource または dict）をキューへ追加。
        再生中でなく一時停止でもなければ、すぐ再生を開始。
        """
        if guild is None or voice_client is None:
            return
        if not voice_client.is_connected():
            return

        queue = _get_queue(guild.id)
        queue.append(audio_entry)

        # 未再生・未一時停止なら開始
        if (not voice_client.is_playing()) and (not voice_client.is_paused()):
            self.play(voice_client, queue)

    def play(self, voice_client: discord.VoiceClient, queue: Deque):
        """
        キュー先頭を取り出して再生。終了時の after で次を呼ぶ。
        """
        # 基本ガード
        if (voice_client is None) or (not voice_client.is_connected()):
            queue.clear()  # 断線していたらキューを捨てる（メモリ保護）
            return
        if not queue:
            return
        if voice_client.is_playing() or voice_client.is_paused():
            return

        item = queue.popleft()

        # dict 形式（{audio, file_path, delete_after_play, debug_used}）と
        # AudioSource 直接の両方をサポート
        if isinstance(item, discord.AudioSource):
            audio: Optional[discord.AudioSource] = item
            file_path: Optional[str] = None
            delete_after_play: bool = False
            debug_used: str = "PCM"
        elif isinstance(item, dict):
            audio = item.get("audio")
            file_path = item.get("file_path")
            delete_after_play = bool(item.get("delete_after_play", False))
            debug_used = item.get("debug_used", "PCM")
        else:
            # 不正なアイテムはスキップして次へ
            print(f"[TTS] スキップ: 不正なエントリ型 {type(item)}")
            loop = _get_loop(voice_client)
            if loop:
                loop.call_soon_threadsafe(self.play, voice_client, queue)
            return

        if audio is None:
            print(f"[TTS] スキップ: audio が None（file_path={file_path}）")
            loop = _get_loop(voice_client)
            if loop:
                loop.call_soon_threadsafe(self.play, voice_client, queue)
            return

        def after_playing(err: Optional[BaseException]):
            """
            注意：音声送出スレッドで実行される。
            ここでは I/O 最小限にし、次の再生はイベントループに委譲。
            """
            if err:
                print(f"[TTS] 再生エラー: {err}")

            # 再生後の一時ファイル削除（必要な場合）
            if delete_after_play and file_path and os.path.exists(file_path):
                try:
                    os.remove(file_path)
                    print(f"[TTS] 削除済み: {file_path}")
                except Exception as e:
                    print(f"[TTS] 削除失敗 {file_path}: {e}")

            # 既に切断されていたらここで終了＆キュー破棄
            if (voice_client is None) or (not voice_client.is_connected()):
                queue.clear()
                return

            loop = _get_loop(voice_client)
            if loop:
                loop.call_soon_threadsafe(self.play, voice_client, queue)

        try:
            print(f"[TTS] 再生開始（{debug_used}）: {file_path or audio}")
            voice_client.play(audio, after=after_playing)
        except Exception as e:
            print(f"[TTS] play() 失敗: {e}")
            # 失敗時も次へ進める
            loop = _get_loop(voice_client)
            if loop:
                loop.call_soon_threadsafe(self.play, voice_client, queue)
