import yaml
from src import logger

class Config:
    discord_access_token = ""
    discord_application_id = ""

    max_text_length = 40
    def __init__(self) :
        try:
            with open("./configs/config.yml") as config_file:
                obj = yaml.safe_load(config_file)
                try:
                    self.discord_access_token = str(obj["access_token"])
                    self.discord_application_id = str(obj["application_id"])
                except:
                    print("")
                try:
                    self.max_text_length = int(obj["max_text_length"])
                except KeyError as e:
                    logger.Error(f"キー {e.__str__()} が config.yml に存在しません。")
                except ValueError as e:
                    logger.Error(f"config.yml の値が不正です。")
        except FileNotFoundError:
            logger.Error("config.yml が存在しません。")
        except yaml.scanner.ScannerError as e:
            logger.Error("config.yml をパースできませんでした。文法に誤りがある可能性があります。")
if __name__ == "__main__":
    Config()
