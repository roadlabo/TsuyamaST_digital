# COMPUTER_SETTING.md：録画用PC・事務所PCセットアップ手順

## 1. システム概要

本システムは、駅前広場に設置したRTSPカメラ映像を、現地建物内の録画用PCでMP4保存するための録画システムである。

録画中のファイルは非共有のTempフォルダに保存し、完成したMP4ファイルだけをArchiveフォルダへ移動する。
VPN経由で事務所PCから閲覧するのはArchiveフォルダのみとする。

## 2. PC構成

### 現地録画用PC

役割：

* RTSPカメラから映像取得
* MP4録画
* Tempフォルダ管理
* Archiveフォルダ作成
* 状態ファイル出力
* 設定ファイル管理
* 事務所PCからのcommand処理

### 事務所PC

役割：

* VPN経由でArchiveフォルダを閲覧
* 完成済みMP4を再生
* status JSONを読み取り状態表示
* 必要に応じてcommandsを作成

## 3. 推奨フォルダ構成

録画用PCのDドライブに以下を作成する。

```text
D:\NVR\
  temp\
  archive\
  config\
  status\
  commands\
    pending\
    processed\
    failed\
  logs\
  quarantine\
```

## 4. 共有設定

VPN経由で共有するのは以下のみ。

```text
D:\NVR\archive
D:\NVR\status
D:\NVR\config
D:\NVR\commands
```

ただし、権限は分ける。

### archive

* 事務所PC：読み取りのみ推奨
* 現地PC：読み書き

### status

* 事務所PC：読み取りのみ
* 現地PC：読み書き

### config

* 事務所PC：読み取りのみ
* 現地PC：読み書き

### commands

* 事務所PC：pendingへの書き込み可
* 現地PC：読み書き可

### temp

共有しない。

```text
D:\NVR\temp
```

このフォルダはVPN経由で見えないようにする。

## 5. Windows設定

### 電源設定

録画用PCは常時稼働させる。

推奨：

* スリープ：しない
* ディスプレイ電源オフ：任意
* HDDスリープ：しない
* Windows Updateによる自動再起動：運用ルールを決める

### 時刻同期

録画ファイル名と証拠性に影響するため、Windows時刻同期を有効にする。

確認項目：

* タイムゾーン：日本
* 時刻同期：有効
* カメラ本体の時刻も可能な範囲で同期

### ネットワーク

* カメラLANと録画用PCが安定して通信できること
* 録画用PCのIPアドレスは固定推奨
* VPN経由でArchive共有にアクセスできること
* RTSPポートへの通信が可能であること

## 6. 必要ソフト

録画用PCに以下を導入する。

* Python 3.11以上
* FFmpeg
* Git
* 必要に応じてVLC

Pythonパッケージ例：

```text
PySide6
portalocker
psutil
pydantic
```

## 7. FFmpeg配置

例：

```text
C:\ffmpeg\bin\ffmpeg.exe
C:\ffmpeg\bin\ffprobe.exe
```

環境変数PATHに以下を追加する。

```text
C:\ffmpeg\bin
```

確認：

```bat
ffmpeg -version
ffprobe -version
```

## 8. アプリ起動

現地PC用：

```bat
python src\main_local.py
```

事務所PC用：

```bat
python src\main_office.py
```

## 9. 自動起動設定

録画用PCでは、Windows起動時に現地PC用アプリを起動する。

方法例：

* スタートアップフォルダにショートカットを置く
* タスクスケジューラを使う

推奨はタスクスケジューラ。

設定例：

* トリガー：ログオン時またはPC起動時
* 操作：pythonw.exeでmain_local.pyを実行
* 最上位の特権で実行
* 失敗時に再試行

## 10. 権限設計

重要：

* tempフォルダは共有しない
* archiveは事務所PCから読み取り専用
* statusは事務所PCから読み取り専用
* configは事務所PCから読み取り専用
* commands/pendingのみ事務所PCから書き込み可

これにより、事務所PCが録画中ファイルや正式設定ファイルを壊すリスクを下げる。

## 11. ファイル競合防止

設計ルール：

* cameras.jsonを正式に書くのは現地PCのみ
* system_status.jsonを書くのは現地PCのみ
* 事務所PCは直接設定ファイルを書き換えない
* 事務所PCからの操作はcommands/pendingにJSONを作る
* 現地PCがcommandsを読み取り、processedまたはfailedに移動する

書き込み処理：

```text
xxx.tmp に書く
↓
書き込み完了
↓
os.replaceで xxx.json に置換
```

## 12. 録画確認

確認項目：

1. カメラ1台でRTSP接続できる
2. tempに録画中ファイルが作成される
3. 区切り後にarchiveへMP4が出る
4. VPN経由でarchiveのMP4が再生できる
5. status JSONが更新される
6. 事務所PC UIで状態が見える
7. MP4区切りcommandが現地PCで処理される

## 13. 運用ルール

### 通常時

* 現地PCアプリを起動したままにする
* 事務所PCからはArchive内の完成MP4を閲覧する
* 録画中ファイルは見ない

### 事故・トラブル時

* 事務所PCまたは現地PCからMP4区切りを実行
* 完成した最新MP4を確認
* 必要なMP4を別フォルダへ保全する

### 保全時

* 該当時間帯の全カメラMP4をコピー
* コピー先は通常削除対象外のフォルダにする
* 可能ならハッシュ値を記録する

## 14. 注意事項

* MP4の分割時刻はキーフレーム位置の影響を受ける場合がある
* カメラ側の設定でGOP間隔を短めにすると区切り精度が上がる可能性がある
* 録画PCの時刻ズレに注意する
* HDD/SSD容量不足に注意する
* Windows Update後の自動再起動に注意する
* 共有フォルダの書き込み権限を広げすぎない

## 15. 初期テスト手順

1. 1台のRTSP URLを登録する
2. 接続テストを行う
3. 1分区切りで試験録画する
4. tempに録画中ファイルが出ることを確認する
5. 1分後にarchiveへMP4が移動することを確認する
6. VPN経由でMP4を再生する
7. status表示を確認する
8. MP4区切りボタンを押す
9. 全カメラ一斉区切りのログを確認する
10. 問題なければ10分区切りに戻す
