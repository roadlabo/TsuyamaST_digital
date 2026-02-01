# TsuyamaST SuperAI Signage Controller Manual

## 目的
事務所PCで 20 台のサイネージPCを集中管理する Controller アプリの運用手順と設定方法をまとめます。

## 配置パス
Controller は以下の構成を想定しています。

```
TsuyamaST_digital\
├─ app\
│  ├─ analysisPC\
│  │  └─ playerTsuyamaST_SuperAI_Signage_Controller.py
│  ├─ config\
│  │  ├─ inventory.json
│  │  ├─ controller_settings.json
│  │  ├─ ai_status.json
│  │  ├─ Sign01\config.json
│  │  └─ Sign01\active.json
├─ content\
│  └─ ch01\...
├─ logs\
├─ runtime\
└─ backup\logs\
```

## 起動方法
1. `runtime\python\python.exe app\analysisPC\playerTsuyamaST_SuperAI_Signage_Controller.py` を実行します。
2. 初回は PySide6 が必要です。`app/signagePC/requirements_controller.txt` に依存ライブラリがあります。

## 画面の見方
- 上部ボタンで一斉操作を実行できます。
- 20 列固定で Sign01〜Sign20 を並べています。
- 状態行: exists / online / enabled / 最終更新 / error を表示します。
- 表示中CH: active.json から算出されたチャンネルを太字表示します。
- AI判定: `ai_status.json` の congestion_level を表示します。
- プレビュー: active channel の `*_sample.mp4` が存在する場合のみ表示します。
- 設定サマリ: sleep/normal/ai/timer を表示します。

## 操作ボタン
### サイネージPC通信確認
- SMB 共有にアクセスできるかを確認します。

### 一斉Ch更新
- 全台の active.json を再計算してローカル更新します。
- 続けて現地へ配布します。

### フォルダ内動画情報取得
- `content/chXX` 内の `*_sample.mp4` を再スキャンします。
- プレビューON/OFF の切替にも使用できます。

### 動画の同期開始
- `content/chXX` を現地へ差分コピーします。
- `staging\sync_tmp` に一度転送し、成功後に `content` へ反映します。

### LOGファイル取得
- 現地 `logs` を `backup\logs\SignXX\YYYYMMDD_HHMMSS` にコピーします。

## command.json 仕様
Controller がサイネージPCへ電源操作を依頼する場合は、`app/config/command.json` に以下の形式で書き込みます。

```
{
  "command_id": "YYYYMMDD_HHMMSS_Sign01",
  "action": "shutdown | reboot",
  "force": true,
  "issued_at": "YYYY-MM-DD HH:MM:SS",
  "by": "controller"
}
```

### 実行条件
- `action` が `shutdown` または `reboot`
- `force` が `true`

### 実行後の処理
- `pc_agent` は `command.json` を `command.done.<epoch>.json` にリネームします。
- 実行結果を `logs/status/command_result.json` に書き込みます。

## 設定変更
1. 各列の「設定」ボタンでダイアログを開きます。
2. enabled, sleep_channel, normal_channel, ai_channels, timer_rules を編集します。
3. 保存すると `config.json` が更新され、active_channel を再計算します。

## AI 判定
- `app/config/ai_status.json` の変更を監視し、更新時に全台再計算します。
- watchdog が無い場合は 10 秒間隔のポーリングで追従します。

## 休眠時間帯
- `controller_settings.json` の `sleep_windows` を編集してください。
- 例: `[{"start":"01:00","end":"04:00"}]`
- 空の場合は休眠判定を行いません。

## 配布に失敗した場合
- エラー表示と「再送」ボタンで再配布できます。
- `inventory.json` の exists=false の列は常にグレーになります。

## ログ
- Controller のログは `logs/controller_YYYYMMDD.log` に出力します。

## トラブルシュート
- 共有名が違う場合は `inventory.json` の share_name を調整してください。
- ネットワークが不安定な場合は `controller_settings.json` の `network_timeout_seconds` を調整します。
