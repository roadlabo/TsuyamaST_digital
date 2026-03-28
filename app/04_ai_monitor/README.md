# 04_ai_monitor (たたき台実装)

## 起動方法
```bash
cd /workspace/TsuyamaST_digital
python app/04_ai_monitor/ai_congestion_monitor.py
```

## 構成
- `ai_congestion_monitor.py`: 統合GUI起動・3カメラ制御・ai_status更新
- `modules/camera_worker.py`: YOLO + ByteTrack推論、通過カウント、長時間滞在判定
- `modules/congestion_logic.py`: 渋滞指数(0-100)
- `modules/counter_logic.py`: 通過ラインと10分ヒストグラム
- `modules/status_manager.py`: `ai_status.json` の安全更新
- `modules/report_writer.py`: 日次/月次Excel出力
- `modules/plot_utils.py`: 多日集計可視化（【脇村モデル】）
- `modules/ui_panels.py`: サイバー風UIパネルと設定ダイアログ
- `config/system_config.json`: システム設定
- `config/camera_settings.json`: カメラ設定

## メモ
- 解析結果動画は保存しません。
- 保存対象はCSV/Excel/JSONのみです。
- 渋滞LEVELは `app/10_common/congestion_common.py` の共通ロジックで判定します（LEVEL1〜4）。
- LONG STAY は内部判定・CSV記録のみで、画面上には表示しません。
- 画面上部情報帯の2段目に、渋滞指数式を1回だけ表示します。
- UIは「動画約80%表示」「右カード小型化」「THラベル左寄せ」を基準に整理しています。
