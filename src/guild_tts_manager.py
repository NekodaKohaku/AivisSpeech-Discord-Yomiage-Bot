from collections import defaultdict, deque
import os
import discord

# ギルドごとの再生キュー
queue_dict = defaultdict(deque)

def _get_queue(guild_id: int) -> deque:
    """ギルドIDに対応する再生キューを取得（なければ作成）"""
    return queue_dict[guild_id]

class guild_tts_manager:
    def __init__(self):
        pass

    def enqueue(self, voice_client: discord.VoiceClient, guild: discord.Guild, audio_entry):
        """
        エントリ（AudioSource または dict）をキューへ追加。
        再生中でなく一時停止でもなければ、すぐ再生を開始。
        """
        if guild is None or voice_client is None:
            return
        queue = _get_queue(guild.id)
        queue.append(audio_entry)
        # 未再生・未一時停止なら開始
        if voice_client.is_connected() and (not voice_client.is_playing()) and (not voice_client.is_paused()):
            self.play(voice_client, queue)

    def play(self, voice_client: discord.VoiceClient, queue: deque):
        """
        キュー先頭を取り出して再生。終了時の after で次を呼ぶ。
        """
        # 基本ガード
        if not voice_client or not voice_client.is_connected():
            # 断線していたらキューを捨てる（メモリ保護）
            queue.clear()
            return
        if not queue:
            return
        if voice_client.is_playing() or voice_client.is_paused():
            return

        item = queue.popleft()

        # dict 形式（{audio, file_path, delete_after_play, debug_used}）と
        # AudioSource 直接の両方をサポート
        if isinstance(item, discord.AudioSource):
            audio = item
            file_path = None
            delete_after_play = False
            debug_used = "UNKNOWN"
        else:
            audio = item.get("audio")
            file_path = item.get("file_path")
            delete_after_play = item.get("delete_after_play", False)
            debug_used = item.get("debug_used", "UNKNOWN")

        if audio is None:
            print(f"[TTS] スキップ: audio が None（file_path={file_path}）")
            # 次のアイテムへ
            loop = getattr(getattr(voice_client, "_state", None), "loop", None)
            if loop:
                loop.call_soon_threadsafe(self.play, voice_client, queue)
            return

        def after_playing(err):
            """
            注意：ffmpeg/voice スレッドで実行される。
            ここでは I/O 最小限にし、次の再生はイベントループに委譲。
            """
            if err:
                print(f"[TTS] 再生エラー: {err}")

            # ソース側の cleanup は discord 側で呼ばれるが、念のためファイル削除をここで実施
            if delete_after_play and file_path and os.path.exists(file_path):
                try:
                    os.remove(file_path)
                    print(f"[TTS] 削除済み: {file_path}")
                except Exception as e:
                    print(f"[TTS] 削除失敗 {file_path}: {e}")

            # 既に切断されていたらここで終了＆キュー破棄
            if not voice_client.is_connected():
                queue.clear()
                return

            loop = getattr(getattr(voice_client, "_state", None), "loop", None)
            if loop:
                loop.call_soon_threadsafe(self.play, voice_client, queue)

        try:
            print(f"[TTS] 再生開始（{debug_used}）: {file_path or audio}")
            voice_client.play(audio, after=after_playing)
        except Exception as e:
            print(f"[TTS] play() 失敗: {e}")
            # 失敗時も次へ進める
            loop = getattr(getattr(voice_client, "_state", None), "loop", None)
            if loop:
                loop.call_soon_threadsafe(self.play, voice_client, queue)
