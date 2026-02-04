# playerTsuyamaST_SuperAI_Signage_Controller

## 盤石型運用アーキテクチャ・設計思想・運用仕様書

### 0. 本書の位置づけ（重要）

本書は、津山駅スーパーAIサイネージシステムにおける
管理PC（コントローラー）＝ `playerTsuyamaST_SuperAI_Signage_Controller.py`
の設計思想・全体構造・データフロー・運用方針を、人が理解できる形で整理したものです。

- 個々の関数実装や例外処理の細部は CODEX に委ねる
- 本書は「なぜこうなっているか」「どこが重要か」を説明することに主眼を置く
- 無人・長期・20台規模運用を前提とした盤石型設計を公式仕様として固定する

### 1. システム全体像（思想レベル）

#### 1-1. 基本思想

本システムは、以下を最優先思想とする。

- 止まらないこと（Fail-Safe / Self-Recovery）
- 中央集権だが、末端は自律的に生きること
- Windows + SMB + Python という“壊れやすい組み合わせ”を前提に設計すること

したがって、

- 一時的な通信断
- ファイル競合
- JSON破損
- Defender / AV / OS都合のロック

が必ず起きる前提で作られている。

### 2. 役割分担（Controller / Signage の責務分離）

#### 2-1. 管理PC（コントローラー）の責務

管理PCは「意思決定と配布」を担う。

- 各サイネージPCの状態を把握する
- 「今どうあるべきか」を判断する
- 設定・コンテンツを押し付ける（配布する）
- ただし、現地の瞬断・遅延・失敗で止まらない

管理PCは状態を読むが、現地の再生そのものは制御しない。

#### 2-2. サイネージPCの責務

サイネージPCは「自律実行と報告」を担う。

- `active.json` に従って再生する
- 再生状態を heartbeat / `pc_status` として出力する
- 管理PCがいなくても再生は継続する

### 3. データフローの核心（ここが最重要）

#### 3-1. 状態監視の流れ（読み取り系）

auto_play.py
   ↓ heartbeat (`auto_play_heartbeat.json`)
pc_agent.py
   ↓ 集約・判断
`pc_status.json`
   ↓（UNC・読み取り）
Controller

管理PCは heartbeat を直接読まない。
唯一の正規監視対象は `pc_status.json`。
heartbeat は「内部材料」であり、外に出さない。

これにより、

- ファイル競合点を最小化
- 監視ロジックをサイネージ側に閉じ込める
- Controller の責務を軽くする

#### 3-2. 設定配布の流れ（書き込み系）

Controller
   ↓ `active.json` 配布（UNC）
Signage PC
   ↓ auto_play が検知
再生チャンネル変更

`active.json` は命令書。
サイネージPCは命令に忠実。
ただし、書き込み失敗はリトライして終わり（全体停止しない）。

#### 3-3. コンテンツ同期の流れ

Controller content/
   ↓ staging (tmp)
   ↓ atomic replace
Signage content/

再生中のファイルに触る可能性があるため、
失敗は「そのファイル単位」で処理し、全体を止めない。

### 4. Windows + SMB 前提の設計上の割り切り

#### 4-1. atomic replace は「失敗するもの」

Windows では以下が頻発する。

- 読み取り中のファイルに replace → WinError 5
- AV が掴む
- UNC の一瞬断

したがって本システムでは、

- replace は必ず失敗する可能性がある
- 失敗したら「短いリトライ → 諦める」
- 諦めてもプロセスは生き続ける

という思想を採用する。

Windows + 共有フォルダ運用では tmp→replace のatomic writeは禁止。上書き＋リトライ方式を採用する。

#### 4-2. JSONは「壊れるもの」

- 書き換え途中を読まれる
- サイズ0
- parse_error

対策：

- 読み取りは必ずリトライ
- 読めなければ「NG扱い」にして進む
- 例外で止めない

### 5. telemetry（監視ループ）の考え方

#### 5-1. なぜ batch / interval が必要か

20台を常時フルスキャンすると、

- SMB競合
- WinError増加
- 無意味な負荷

を招く。

そのため、

- OK台はゆっくり
- NG台はやや頻繁
- batch で分散

という「生き物的な監視」を行う。

### 6. controller_settings.json の思想的位置づけ

`controller_settings.json` は「速さ」ではなく「壊れにくさ」を調整するダイヤルである。

- thread_workers：並列数 = 事故率
- telemetry_interval：読み頻度 = replace失敗率
- timeout：誤NG率

現地ネットワーク・PC性能に応じて調整する。

### 7. CODEXに委ねる範囲（明示）

以下の事項は設計思想は固定し、実装詳細は CODEX に委ねる。

#### 7-1. 委任事項

- safe_replace / safe_read_json の具体実装
- retry回数・sleep時間の微調整
- ログ文言の粒度
- UI表示の細かな表現
- 今後の拡張（台数増加・AI判定追加）

#### 7-2. 委任の原則

CODEXは以下を破ってはならない。

- 例外でプロセスを落とさない
- UNC I/O を一発勝負にしない
- heartbeat を直接 Controller が読む設計に戻さない
- 「速さ優先」にしない

### 8. 運用上の最重要チェックポイント

- `pc_status.json` が更新され続けているか
- Controller が落ちていないか
- `active.json` が配布できなくても再生は続くか
- NGが一時的で、自然復帰するか

### 9. 本書の使い方（実務）

- 庁内説明：第1〜3章
- 引き継ぎ：第3〜5章
- 外注・委託：第7章（CODEX委任ルール）
- 障害対応：第4・8章

### 10. 最終宣言（仕様固定）

本システムは、

「Windowsは壊れる、SMBは信用しない、
それでも止まらず動き続ける」

という思想を正式仕様とする。
