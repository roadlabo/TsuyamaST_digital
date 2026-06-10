# RTSP入力MP4録画システム PC設定手順

## 1. フォルダ作成

録画用PCで以下を作成します。`archive` のみVPN共有対象にしてください。

```text
D:\NVR\
  temp\
  archive\
  config\
  status\
  commands\pending\
  commands\processed\
  commands\failed\
  logs\
```

## 2. FFmpeg導入

1. Windows用FFmpegを導入します。
2. `ffmpeg.exe` と `ffprobe.exe` をPATHに追加するか、`D:\NVR\config\app_settings.json` の `ffmpeg_path` / `ffprobe_path` にフルパスを設定します。

## 3. 共有設定

- VPN共有するフォルダは `D:\NVR\archive` を原則読み取り専用にします。
- 事務所PCから状態確認や操作依頼を行う場合のみ、`status` は読み取り、`commands\pending` は書き込み可能にします。
- `temp` は共有しません。録画中ファイル（`.partial`）を共有側に見せないためです。

## 4. 起動

現地PCでは以下を実行します。

```bat
python app\05_nvr_recorder\start_local_recorder.py
```

事務所PCでは共有パスを指定して以下を実行します。

```bat
python app\05_nvr_recorder\start_office_monitor.py
```

## 5. 注意

- 設定ファイルと状態ファイルは一時ファイルへ書いてから `os.replace` で置換します。
- 現地PCだけが `cameras.json` と `status` を正式に書きます。
- 起動時に残っている `.partial` は `temp\_quarantine` へ隔離されます。
