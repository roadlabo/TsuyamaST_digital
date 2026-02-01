# HWiNFO 年次ログ (pc_agent 組み込み)

## HWiNFO 側の設定
1. HWiNFO を起動し、Logging を有効化します。
2. 保存先を次に設定します。
   - `C:\_TsuyamaSignage\logs\hwinfo\hwinfo_sensors.csv`
3. ログ間隔（ポーリング ピリオド）は既存運用に合わせます（例: 10 秒）。

## 年次ログの仕様
- 30 分に 1 回、最新の HWiNFO 行から必要最小限の列だけを採取して年次 CSV に追記します。
- 保存先:
  - `C:\_TsuyamaSignage\logs\hwinfo\yearly\hwinfo_YYYY.csv`
- ヘッダー（固定）:
  - `日時,CPU使用率[%],CPU温度[℃],チップセット温度[℃],CPU内GPU温度[℃],SSD温度[℃],メモリ温度[℃],Cドライブ総容量[GB],Cドライブ空き容量[GB]`
- 日時フォーマット: `YYYY/MM/DD HH:MM`
- C ドライブ容量は OS から取得（`shutil.disk_usage("C:\\")`）

### サンプリング間隔の一時変更
- 環境変数で上書きできます。
  - 例: `HWINFO_SAMPLE_MINUTES=1`

## 無人運用のポイント
- 入力ファイル `hwinfo_sensors.csv` は削除・リネームしません。
- サイズが 1MB を超えると中身だけを truncate して肥大化を防止します。
- 欠損値がある行は年次 CSV に書き込みません。
