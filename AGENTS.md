# AGENTS.md —— 本工作区 Agent 工作约定

> 适用对象：在本仓库（`D:\working_space\MyLocalAI`）中做任何开发、改动工作的 AI Agent。
> 本文件优先级高于你的默认习惯；与用户当次明确指令冲突时，**以用户指令为准**。

---

## 1. 铁律：功能更新一律先建 git 分支

**在动手改代码做功能更新之前，先自己创建一个 git 分支，不要直接在主线（`master` / 远程主线）上改。**

- 只读操作（看代码、查资料、跑一次测试、分析日志）**不需要**建分支。
- 纯文字/文档微调（README 错别字、注释说明）可以不建分支；但只要动到 `src/`、`scripts/`、`tests/`、配置 JSON 或前端静态资源，就按功能更新处理，必须建分支。
- 不确定算不算功能更新，就建分支——多建一个分支的成本远低于搞脏主线。

### 新分支的基线：先 `master`，没有就用远程主线

**新分支必须从主线拉出。不要从别人的在途分支、自己的旧分支、或随手某个 commit 拉出。**

按顺序选基线，**第一条能用就用第一条**：

1. **本地有 `master`** → 就用 `master`（首选）。
   ```powershell
   git switch master
   git pull --ff-only              # 有远程才执行
   git switch -c feature/<描述>    # 从 master 拉出新分支
   ```
2. **本地没有 `master`** → 用**远程仓库的主线**，也就是 `origin/HEAD` 指向的那条分支。
   它不一定叫 `master`（本仓库当前是 `agent-claim-checking-v02`），所以**不要写死分支名**：
   ```powershell
   git fetch origin --prune
   git switch --no-track -c feature/<描述> origin/HEAD   # 直接以远程主线为基线
   ```
   `--no-track` **必须带**：从远程分支拉新分支时，git 默认会把新分支的上游（upstream）
   设成 `origin/HEAD` 指向的那条主线，之后 `git push` 就会直接推到主线上。
   `origin/HEAD` 没设置时，先 `git remote set-head origin -a` 让它指向远程默认分支；
   或先 `git branch -r` 看清主线名，再显式写成 `origin/<远程主线名>`。
3. **既没有本地 `master`，也没有可用的远程主线** → **停下来问用户**以哪条线为基线，不要自己拍一个。

> 判断基线是否存在的常用命令：
> ```powershell
> git branch --list master                    # 本地有没有 master
> git symbolic-ref refs/remotes/origin/HEAD   # 远程主线是哪条
> git branch -r                               # 远程分支清单
> ```

### 用户点名了分支 → 就在那个分支工作（优先级最高）

**只要用户说了"在 xxx 分支上做""切到 xxx 分支""用 xxx 分支"，就以用户的指定为准，不要自己另建分支、也不要改名。**

- 分支**已存在**：先 `git branch --show-current` 确认，再 `git switch <用户指定的分支名>`，然后在该分支上完成全部改动和提交。
- 分支**不存在**：按用户给的名字原样创建（名字不合下面的命名规范也照用，用户的命名优先）：
  ```powershell
  git switch -c <用户指定的分支名>     # 基线：master → 远程主线 → 用户指定
  ```
  并在最终答复里说明"该分支此前不存在，已新建"。
- **不要**擅自把用户的改动挪到别的分支、不要改分支名、不要 `git switch` 回 `master` 继续做（除非用户这么说）。
- 用户指定了分支时，**默认只在该分支上提交，不自动合并回 `master`**；要不要合并、什么时候合并，等用户发话。
- 切换分支时如果遇到"本地改动会被覆盖"之类的冲突，**停下来问用户**，不要用 `stash` / `checkout -- .` 强行清场。
- 用户指定的分支如果看起来像别人的在途分支（比如已被其他 agent/任务占用），先确认一句再动手，不要在别人分支上叠加自己的提交。

### 用户没指定分支 → 自己建（基线见上、命名规范见下）

```powershell
# 在仓库根目录，先确认当前状态
git status --short
git branch --show-current

# 再按上文顺序选基线：
# 1) 本地有 master —— 首选
git switch master
git pull --ff-only                       # 有远程才执行
git switch -c feature/<简短英文描述>     # 从 master 拉出

# 2) 本地没有 master —— 以远程主线 origin/HEAD 为基线
git fetch origin --prune
git switch --no-track -c feature/<简短英文描述> origin/HEAD

# 3) 两者都没有 —— 停下来问用户，不要自己拍基线
```

### 分支命名规范

| 类型 | 前缀 | 示例 |
| --- | --- | --- |
| 新功能 / 能力增强 | `feature/` | `feature/attention-calibration` |
| 缺陷修复 | `fix/` | `fix/router-empty-intent` |
| 重构（行为不变） | `refactor/` | `refactor/dossier-migrations` |
| 文档 / 配置 / 脚本 | `docs/` | `docs/agents-branch-rule` |
| 实验性尝试（可丢弃） | `exp/` | `exp/deep-read-guard-v2` |

- 描述部分用**英文小写 kebab-case**（2–5 个词），避免中文、空格、日期堆砌，保证跨平台和命令行可用。
- 分支一律从**主线**拉出：**本地有 `master` 就用 `master`，没有就用远程主线 `origin/HEAD`**（见上文"新分支的基线"）；主线前进很多时先 `git pull --ff-only` 再建分支。
- 一个分支只做一件事：不要把互不相关的改动塞进同一个分支。

---

## 2. 保护现有工作区（本仓库尤其重要）

本仓库的主线（`master` / 远程主线）上**长期存在大量未提交改动**（历史遗留 dirty working tree）。请严格遵守：

- **禁止**在未获得用户明确许可时执行任何破坏性命令：
  `git checkout -- .`、`git restore .`、`git stash`、`git reset --hard`、`git clean -fd`、`git checkout <branch> -- .`。
- 开分支不会丢改动：未提交的改动会跟着工作区一起走，`git switch -c` 是安全的。
- 提交时**只 `git add` 本次任务涉及的文件**（精确列路径），严禁 `git add -A` / `git add .` 把无关脏文件一起提交。
- 不要顺手格式化、重排、删除与本任务无关的文件。
- 不要为了"干净"去改写历史（`rebase`、`commit --amend` 已推送的提交等）。

---

## 3. 收尾：验证、提交、合并、推送

1. **验证**（在仓库根目录，使用项目自带的 `.venv`）：
   ```powershell
   .venv\Scripts\python.exe -X utf8 -m unittest discover -s tests -p "test_*.py"
   scripts\ci.cmd        # 一键 CI：compileall + unittest + mock 闸门
   ```
   改动涉及前端页面时，除单元测试外还要按 `my_website/start.bat` 起服务实际看一眼页面。
2. **提交**：沿用本仓库既有风格，提交信息带模块/阶段前缀，正文说明"改了什么、为什么、怎么验证"。
   ```text
   fix: 路由空意图兜底，避免空字符串进 chat_json
   ```
3. **合并**：
   - **用户说了"合并"，但没说合到哪**（默认解释，按此执行）：
     **默认合并进 `master`；本地没有 `master`，就合并进远程仓库的主线**（`origin/HEAD` 指向的那条）。
     不要因为"没说清"就停下来反问——**只有**本地没有 `master`、且拿不到远程主线（`origin/HEAD`
     没设置也没有 `origin/*` 可参照）时，才停下来问用户合到哪。
     ```powershell
     # 情况 A：本地有 master
     git switch master
     git merge --no-ff <你的分支>
     git push origin master

     # 情况 B：本地没有 master（本仓库就是这种）——合进远程主线
     git fetch origin --prune
     # 若已有跟踪远程主线的本地分支，直接 switch 过去；没有才新建：
     git switch --no-track -c __merge-<任务> origin/HEAD   # 从远程主线出合并点
     git merge --no-ff <你的分支>
     git push origin HEAD:refs/heads/<远程主线名>           # 推到远程主线
     ```
     合并完可在报告里说明这条本地临时分支是否保留。
   - **合并必须"本地 + 远程"一起做，不能只动远程**（见下面第 5 条）：推完远程主线后，
     **本地主线分支也要指向同一个 commit**，`git status -sb` 不应残留 ahead/behind。
   - **用户没指定分支**（分支是你自己建的）：默认做完并自检通过后按上面同一条规则合并，再回到最终答案里报告分支名、commit 哈希、是否已合并、测试/CI 结果。
   - **用户指定了分支**：默认**不合并**，改动和提交都留在用户指定的那条分支上，等用户指示；不要在用户没要求时把它合进主线。
   - **用户明确说了"留在这个分支 / 先别合并 / 等我看"**：不要合并，也不要删除分支，交由用户处理。
   - **合并前先确认没有冲突风险**：`git switch` 到目标主线时若提示"本地改动会被覆盖"，**停下来问用户**，不要用 `stash` / `checkout -- .` 清场。
   - **合并后报告实际合入的那条线**：明确写出合进了 `master` 还是哪条远程主线，别只说"已合并"。
4. **推送：代码写完、自检通过、提交完成后，主动 push，不要等用户催。**
   - **先确认有没有远程**：`git remote`。
     - **没有远程**：跳过推送，在最终答复里明确写一句"本仓库未配置远程，已跳过推送，改动仅在本地"——不要说"已推送"。
     - **有远程**：继续下面的步骤。
   - 推送**当前分支**并建立跟踪：
     ```powershell
     git push -u origin <当前分支名>
     ```
     远程名可能不叫 `origin`，用 `git remote` 查到的名字替换。
   - **合并回主线后，再推主线**（保持主线有远程备份）：
     ```powershell
     git push origin <主线分支名>     # 本地有 master 就是 master，否则是 origin/HEAD 指向的那条
     ```
   - **禁止 `git push --force` / `--force-with-lease`**，除非用户明确要求，且确认只影响自己尚未共享的分支；不确定就停下来问。
   - 推送前先看清要推的文件：`git status --short` 与 `git log origin/<branch>..HEAD --oneline`（若远程已有该分支）。**绝不把密钥、`.env`、本地数据库、日志、`archive/`、`.tmp/` 等本地资产推上去**。
   - 推送前若远程有别人的新提交：`git pull --rebase` 拉平后再推（仅限自己分支且未共享；否则停下来问用户），**不要为了"推上去"而 force push 覆盖别人的提交**。
   - 推送失败（无权限、网络、SSH key、远端拒绝）时：如实报告失败原因和本地提交仍在，**不要为了绕过失败而改写提交或强推**。
5. **本地与远程必须一起操作（硬性）—— 别只动一半**
   **任何推送/合并完成后，本地分支和远程分支必须指向同一个 commit。**
   只在本地合并、不推远程（或只推远程、本地分支还停在旧提交）都算**没做完**。
   ```powershell
   # 合并并推到远程主线之后，把本地主线分支也对齐到同一个 commit
   git fetch origin --prune
   git branch -f <本地主线分支名> origin/<远程主线名>   # 仅当它没被任何 worktree 占用时
   # 若本地主线分支正被某个 worktree 占用（git 会拒绝 -f），就改在该 worktree 里
   #   git -C <该 worktree 路径> reset --hard origin/<远程主线名>   ← 有未提交改动时先问用户
   ```
   收尾时必须核对三条，任一不满足就说明没做完：
   ```powershell
   # ① 本地主线 vs 远程主线：不应有 ahead/behind
   git rev-parse <本地主线分支名> origin/<远程主线名>
   # ② 工作分支状态
   git status -sb
   # ③ 工作区是否干净（本仓库长期脏，只确认本次涉及的文件）
   git status --short -- <本次改动的文件>
   ```
   - **`git branch -f` / `reset --hard` 只用于对齐主线**，且**必须先用 `git worktree list`
     确认目标分支没有被别的 worktree 占用**；被占用时不要强推状态，改到该 worktree 内操作或停下来问用户。
   - **禁止**用上面这两条命令去"清理"别的东西；它们只服务于"本地对齐远程主线"这一个目的。
   - 最终答复里要写明：本地主线分支现在指向哪个 commit、远程主线指向哪个 commit、两者是否已一致。
6. 合并后不要删除用户可能还要看的分支；除非用户明确说可以删。

---

## 3.5 文档同步：改完代码必须更新 README.md

**只要本次改动影响了外部可观察的行为，收尾时必须同步更新仓库根目录的 `README.md`，并随代码一起提交、推送。**

- **必须更新 README.md 的情形**（命中任一即算）：
  - 新增/删除/改名了命令行参数、入口命令、环境变量；
  - 新增/删除/改名了 HTTP 接口、MCP 工具、对外暴露的函数或配置项；
  - 目录结构发生变化（新增模块/包/目录）；
  - 新增依赖、变更安装或启动步骤；
  - 阈值/默认值/档位等对使用者可见的配置默认值变了；
  - 修复了 README 里已经写错的行为描述。
- **不必更新**：纯内部重构（行为不变）、只改测试、只改注释/格式、只在 `AGENTS.md` 之类的 Agent 约定文件里改动。
- **怎么改**：README.md 按模块分节（目录结构 / 快速开始 / 各能力章节 / 测试 / 已知局限 / 文档索引），
  改动要落到**对应那节**，不要另起无关章节，也不要重排既有结构。新特性若已有对应章节，
  续写在该章节内；确实没有归属时才新增一节，并同步更新"目录结构"里的文件树。
- **顺带检查多级 README**：`src/mylocalai/rumor/README.md` 等子级 README 若涉及被改动的能力，
  一并更新；`docs/` 下引用了旧行为/旧命令的文档也要检查。
- **提交方式**：README 改动与代码改动放在**同一个分支**、**同一次或紧邻的提交**里，
  不要留下"代码已合、README 还没改"的状态。提交信息里点明文档跟进了什么。
- **收尾自检**：
  ```powershell
  # 本次改了哪些文件；如果只看到 src/ 没看到 README.md，先停下来想想是否漏了文档
  git status --short
  git diff --stat
  ```
  若判断确实不需要改 README，**在最终答复里用一句话说明理由**（例如"纯内部重构，README 无需变更"），
  不要默默跳过。

---

## 4. 并行协作提醒

本工作区可能同时有多个 agent 在改代码。分支隔离是避免互相踩踏的第一道防线，因此：

- 动手前先 `git branch --show-current` 确认自己在哪个分支、有没有别的 agent 已建过同名分支。
- 若发现当前分支已被其他并行任务占用（同名/同名提交），另起自己的分支，不要强行复用；**但用户明确指定了分支时以用户为准**，先确认一句再动手。
- 改动范围写清楚在最终答复里（哪些文件、哪些分支），方便主线汇总。

---

## 5. 一句话速查

> **建分支先选基线：本地有 `master` 就从 `master` 拉；没有就从远程主线 `origin/HEAD` 拉（记得 `--no-track`，否则 push 会打到主线上）；两者都没有就问用户，别自己拍。**
> **用户只说"合并" → 默认合进 `master`；本地没有 `master` 就合进远程主线 `origin/HEAD`，别反问。**
> **用户说了分支 → 就 `git switch <该分支>`（不存在就按原名建、基线同上）在该分支做完，不擅自合并回主线。**
> **用户没说 → 自己 `git switch -c feature/xxx`（基线同上）→ 只改相关文件 → 跑 CI → 精确 `git add` 提交 → 合并回主线并报告分支名。**
> **写完必推：提交后 `git push -u origin <当前分支>`，合并后再推主线；没有远程就跳过并在答复里说明"未配置远程，改动仅在本地"。**
> **本地远程一起动：推完/合完必须让本地分支和远程分支指向同一个 commit，只做一半算没做完。**
> **改完代码必须更新 README.md：行为、接口、参数、目录、依赖、默认值有变就同步改对应章节，随代码一起提交推送；确实不需要改就在答复里说明理由。**
> **永远不要用 `git reset --hard` / `git clean` / `git stash` 清理工作区，也不要 `git add -A`，不要 `git push --force`。**
