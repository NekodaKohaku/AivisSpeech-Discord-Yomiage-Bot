# はじめに
Bot実行までの手順を解説します。AIボイスチェンジャー機能を使うかどうかで、セットアップ方法が変わります。


# 依存関係
読み上げボットを最低限動作させるために、以下のソフトウェアが必要です。
 - ffmpeg
 - voicevox
 - python3

# 動作環境
Linux をサポートしています。


## 読み上げボット(本プログラム)のセットアップ
```shell
git clone https://github.com/NekodaKohaku/AivisSpeech-Discord-Yomiage-Bot.git
cd AivisSpeech-Discord-Yomiage-Bot
sh setup.sh
```

## python3 のインストール
```shell
sudo apt update
sudo apt install python3 python3-pip
```


### 仮想環境作成とpip依存関係解消
仮想環境(venv)をセットアップします。以下のコマンドで、プロジェクトファイル下に.venvファイルが作成されます。  
仮想環境は`source .venv/bin/activate`で有効にできます。
```
python3 -m venv .venv

source .venv/bin/activate
pip3 install -r requirements.txt
```


## ffmpegのインストール
```shell
sudo apt update
sudo apt install ffmpeg
```

## VoiceVoxのインストール
```shell
docker pull ghcr.io/aivis-project/aivisspeech-engine:cpu-latest
```


# 構成ファイルの設定
## DiscordのAPI設定
 1.Discordの[開発ポータル](https://discord.com/developers/applications)にアクセスします。  
 2.そこから新規アプリ作成し、ApplicationIDとTokenをメモします。  
 3.Botというタブから「PRESENCE INTENT」,「SERVER MEMBERS INTENT」,「MESSAGE CONTENT INTENT」をすべてオンにします。  
 4.configs/config.ymlをテキストエディタで開き、「access_token」、「application_id」をそれぞれ、先程メモしたTokenとApplicationIDに書き換えます。  
```
access_token: DISCORD_ACCESS_TOKEN
client_id: APPLICATION_ID
```
 <br>
 これでDiscordの設定は完了です。


 
# 実行
``` shell
# AivisSpeech Engineの起動
docker run -d --rm -p '10101:10101' \
  -v ~/.local/share/AivisSpeech-Engine:/home/user/.local/share/AivisSpeech-Engine-Dev \
  ghcr.io/aivis-project/aivisspeech-engine:cpu-latest

# Discord botの起動
cd path/to/yomiage-bot #プロジェクトファイルに移動
source .venv/bin/activate # 仮想環境を有効化
python3 main.py # DiscordBotクライアントの起動
```
