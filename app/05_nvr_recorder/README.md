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

録画データの保存先はリポジトリ内ではなく、運用PC上のフォルダです。

## 保存先の考え方

現地PCのUIで、次の2つのフォルダを分けて設定できます。

| 用途 | 推奨先 | 内容 |
|---|---|---|
| 一時・ログ・状態フォルダ | 内蔵SSD | 録画中の一時ファイル、ログ、status、commands、quarantine |
| 完成MP4保存フォルダ | 外付けHDD | 完成したMP4を保存する `archive` |

例：

```text
D:\NVR          ← 一時・ログ・状態フォルダ（内蔵SSD）
E:\NVR          ← 完成MP4保存フォルダ（外付けHDD）
```

この場合、録画中ファイルは `D:\NVR\temp` に作られ、区切り完了後に `E:\NVR\archive` へ移動します。

カメラ設定とアプリ設定ファイルは、起動時に確実に読めるよう既定の `D:\NVR\config` に保持します。

## 容量管理

保存期間の日数による削除は行いません。
完成MP4保存フォルダの空き容量が約5TBを下回ると、完成MP4を古いものから自動削除します。

## 自己診断

録画停止や極端に小さいMP4が続く場合は、`docs/SELF_DIAGNOSIS.md` を確認してください。
