# RTSP入力MP4録画システム 操作手順

## 現地PC UI

1. `python app\05_nvr_recorder\start_local_recorder.py` を起動します。
2. カメラ一覧から対象IDを選択します。
3. カメラ名、RTSP URL、有効/無効、保存サブdir、区切り分、保存日数を入力します。
4. 「設定保存」を押します。
5. 「接続テスト」でRTSP疎通を確認します。
6. 「全録画開始」または「個別開始」で録画します。
7. 「全カメラMP4区切り」で現在の録画区間を確定し、直後に次区間を開始します。
8. 「全録画停止」または「個別停止」で録画を止めます。

## 事務所PC UI

1. `python app\05_nvr_recorder\start_office_monitor.py` を起動します。
2. 共有された `status` / `config` / `commands` のパスを指定します。
3. 状態JSONを読み、録画状態、最終ファイル、空き容量、最終エラーを確認します。
4. 操作が必要な場合は「MP4区切り依頼」「全録画開始依頼」「全録画停止依頼」を押します。
5. UIは `commands\pending` にJSON命令ファイルを作成するだけで、`cameras.json` を直接変更しません。

## ファイルの流れ

録画中は非共有のTempへ `.partial` として保存します。

```text
D:\NVR\temp\cam01\recording_20260610_090000.partial
```

区切り完了後、完成MP4だけをArchiveへ移動します。

```text
D:\NVR\archive\cam01\2026-06-10\cam01_20260610_090000_091000.mp4
```

## トラブル対応

- `FFmpegが見つかりません`: `app_settings.json` のパスまたはPATHを確認します。
- `状態JSON読込エラー`: 直前の正常データを表示し続けます。現地PC側ログを確認してください。
- 空き容量不足: 保存日数を短くするか、「容量整理」を実行します。
