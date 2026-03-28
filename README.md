# TsuyamaST Signage

## 方針（この構成が正）
- **GitHub / マスターUSB / 現地PC は同一構成**。
- **Python は2本のみ**。
  - 再生: `app/01_signagePC/auto_play.py`
  - PC管理: `app/01_signagePC/pc_agent.py`
    - 再起動／シャットダウン
    - 死活監視／再生監視（heartbeat）
    - 負荷／容量監視

## フォルダ構成（GitHub/USB/現場で完全一致）
```
TsuyamaST_digital\
  app\
    01_signagePC\
    02_SignageController\
    03_ip_camera_viewer\
    04_ai_monitor\
    10_common\
    11_config\
    90_sample\
  content\
  docs\
  logs\
  runtime\
    python\
    mpv\
```

## 管理番号を付与した理由
- 起動順と依存関係をフォルダ名だけで判別しやすくするため。
- 運用時に確認対象を固定し、保守ミスを減らすため。

## 起動順の想定
1. `01_signagePC`（親機・常駐）
2. `04_ai_monitor`（AI監視）
3. `02_SignageController`（全体制御）
4. `03_ip_camera_viewer`（カメラ確認）

## 旧フォルダ名との対応表
| 旧フォルダ名 | 新フォルダ名 |
|---|---|
| signagePC | 01_signagePC |
| SignageController | 02_SignageController |
| ip_camera_viewer | 03_ip_camera_viewer |
| ai_monitor | 04_ai_monitor |
| common | 10_common |
| config | 11_config |
| sample | 90_sample |

## USB更新手順（GitHub → USB → 現場）
1. GitHub を clone して最新のリポジトリを取得する。
2. 取得したフォルダを **USBのルート** に `_TsuyamaSignage` として配置する。
3. 現地PCの `_TsuyamaSignage` を **USBの `_TsuyamaSignage` で上書き** する。
4. `_TsuyamaSignage` の構成が上記と一致していることを確認する。

## 重要な前提
- **GitHub / マスターUSB / 現地PC の構成は完全一致** させてください。
- runtime 配下には必ず以下を配置する。
  - `runtime/python/python.exe`
  - `runtime/mpv/mpv.exe`

## 動画同期の方式（ミラー同期）
- **全消去全転送ではありません**。差分のみコピーします。
- **マスターに無いファイルは削除**します（ADD/UPD/DEL のミラー同期）。
- **mtime/ctime/size が違う場合は差し換え**します（`compare_ctime` 設定で挙動を切替）。

## Controller 起動（タスクスケジューラ）

起動は C:\_TsuyamaSignage\start_controller.bat

venv固定：C:\_TsuyamaSignage\runtime\venv\Scripts\python(w).exe

ログ：C:\_TsuyamaSignage\logs\controller_start_*.log

## AI Monitor / SignageController の渋滞表示ルール（統一仕様）
- 渋滞レベルは **渋滞LEVEL1〜渋滞LEVEL4** の4段階固定。
  - LEVEL1: 通常時
  - LEVEL2: 軽度渋滞
  - LEVEL3: 中度渋滞
  - LEVEL4: 重度渋滞
- LEVEL判定は `app/10_common/congestion_common.py` の共通関数で統一。
  - `congestion_level_from_index(...)`
  - `compute_level_from_status(...)`
- しきい値は config 側（`level2_threshold`, `level3_threshold`, `level4_threshold`）から読み取り可能。判定処理自体は common 側のみで実行。
- LONG STAY は内部判定・記録のみ（画面表示しない）。
- 渋滞指数の式説明は、画面上部情報帯の2段目に1回だけ表示（カメラごとの重複表示はしない）。
- 渋滞指数は各カメラで独立計算し、台数差（ライン通過カウント）は従来どおり保持する。
- 渋滞指数は各フレームで `Σ[1 / (1 + (d / W) × 500)]` を算出し、3秒窓で蓄積した合計を同窓の有効フレーム数で割ったフレーム平均値とする（3秒ごと更新）。
- FPS変動の影響を抑えるため、秒割りではなくフレーム平均で算出する。
- `congestion_calculation_interval` の推奨値は3秒。既存JSON設定に過去値（例:10秒）がある場合はUIで変更する。
- 指標定義の変更に伴い `congestion_threshold` は再調整が必要。
- UI整理方針
  - 動画表示を約80%スケールで表示（アスペクト比維持）
  - 各カメラ右側カードを小型化（横並び運用を見据えたコンパクト表示）
  - 渋滞指数グラフのTHラベル/凡例は左側表示に統一し、右端の重なりを回避
