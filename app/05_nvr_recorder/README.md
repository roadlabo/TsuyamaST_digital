# 05_nvr_recorder

RTSPカメラをMP4録画するための専用フォルダです。
普段見るファイルは、下の2つだけです。

## 起動ファイル

| PC | 起動するファイル |
|---|---|
| 録画用PC | `start_local_recorder.py` |
| 事務所PC | `start_office_monitor.py` |

## 起動コマンド

録画用PC:

```bat
python app\05_nvr_recorder\start_local_recorder.py
```

事務所PC:

```bat
python app\05_nvr_recorder\start_office_monitor.py
```

## フォルダ構成

```text
app\05_nvr_recorder\
  start_local_recorder.py   起動用: 録画用PC
  start_office_monitor.py   起動用: 事務所PC
  recorder\                 内部処理
  config\                   内部処理
  status\                   内部処理
  commands\                 内部処理
  utils\                    内部処理
```

録画データの保存先はリポジトリ内ではなく、運用PCの `D:\NVR\` です。
