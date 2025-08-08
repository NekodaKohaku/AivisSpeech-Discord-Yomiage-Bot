from collections import defaultdict, deque
import os

queue_dict = defaultdict(deque)

class guild_tts_manager:
    def __init__(self):
        pass

    def enqueue(self, voice_client, guild, source):
        queue = queue_dict[guild.id]
        queue.append(source)
        if not voice_client.is_playing():
            self.play(voice_client, queue)

    def play(self, voice_client, queue):
        if not queue or voice_client.is_playing():
            return

        source_info = queue.popleft()

        if isinstance(source_info, dict):
            audio_source = source_info["audio"]
            file_path = source_info.get("file_path")
            delete_after_play = source_info.get("delete_after_play", False)
        else:
            audio_source = source_info
            file_path = None
            delete_after_play = False

        def after_play(err):
            if delete_after_play and file_path and os.path.exists(file_path):
                try:
                    os.remove(file_path)
                except Exception as e:
                    print(f"ファイル削除失敗: {file_path} - {e}")
            self.play(voice_client, queue)

        voice_client.play(audio_source, after=after_play)
