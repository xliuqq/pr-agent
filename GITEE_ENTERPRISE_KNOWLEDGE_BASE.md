# Gitee 企业版 API 知识库

> **最后更新**: 2026-05-20  
> **API 版本**: v8  
> **Provider**: `GiteeEnterpriseProvider`  
> **官方文档**: https://gitee.com/api/v8/swagger

---

## 📋 目录

1. [基础配置](#基础配置)
2. [URL 格式规范](#url-格式规范)
3. [API 端点清单](#api-端点清单)
4. [响应数据结构](#响应数据结构)
5. [已知限制与降级策略](#已知限制与降级策略)
6. [实现要点](#实现要点)
7. [常见问题 FAQ](#常见问题-faq)

---

## 🔑 基础配置

### 必填配置项

在 `settings_prod/.secrets.toml` 中配置：

```toml
[gitee_enterprise]
url = "http://your-gitee-enterprise-domain"  # 企业版服务器地址，必填
personal_access_token = "your-token"          # 个人访问令牌，必填
```

**重要提示**：
- ❌ 没有默认值
- ❌ 不允许为空
- ✅ 必须在配置文件中显式设置
- ✅ 配置错误会直接报错提示

### 认证方式

```python
headers = {
    "Authorization": f"token {access_token}",
    "Content-Type": "application/json",
    "Accept": "application/json"
}
```

对于 GET 请求，`access_token` 也会作为 query parameter 自动添加。

---

## 🌐 URL 格式规范

### PR URL 格式

```
http://{base_url}/enterprises/{enterprise_id}/projects/{project_id}/pull_requests/{pr_number}
```

**示例**：
```
http://gitee.wty.cn/enterprises/2/projects/208/pull_requests/66
```

### Issue URL 格式

```
http://{base_url}/enterprises/{enterprise_id}/projects/{project_id}/issues/{issue_number}
```

---

## 📡 API 端点清单

### ⚠️ 重要发现：路径不一致性

Gitee 企业版 API v8 存在**路径不一致**的问题：

| 资源类型 | 读取/列表接口 | 评论接口 | 说明 |
|---------|-------------|---------|------|
| Pull Requests | `/pull_requests/{id}` | `/pull_requests/{id}/notes` | ❗ **注意** |
| Issues | `/issues/{id}` | `/issues/{id}/comments` | ✅ 一致 |
| Files | `/pull_requests/{id}/files` | - | 使用 pull_requests |
| Commits | `/pull_requests/{id}/commits` | - | 使用 pull_requests |
| Labels | `/pull_requests/{id}/labels` | - | 使用 pull_requests |

### 1. Pull Request 相关

#### 1.1 获取 PR 详情
- **方法**: `GET`
- **端点**: `/enterprises/{enterprise_id}/projects/{project_id}/pull_requests/{pr_number}`
- **用途**: 获取 PR 的详细信息（标题、描述、状态、分支等）
- **实现位置**: `_fetch_pr_data()` (line 302)

#### 1.2 获取 PR 文件列表
- **方法**: `GET`
- **端点**: `/enterprises/{enterprise_id}/projects/{project_id}/pull_requests/{pr_number}/files`
- **用途**: 获取 PR 中所有变更的文件列表及差异信息
- **实现位置**: `_fetch_pr_data()` (line 338)

#### 1.3 获取 PR Commits
- **方法**: `GET`
- **端点**: `/enterprises/{enterprise_id}/projects/{project_id}/pull_requests/{pr_number}/commits`
- **用途**: 获取 PR 中的所有提交记录
- **实现位置**: `_fetch_pr_data()` (line 367)

#### 1.4 更新 PR 标题和描述
- **方法**: `PATCH`
- **端点**: `/enterprises/{enterprise_id}/projects/{project_id}/pull_requests/{pr_number}`
- **用途**: 更新 PR 的标题和描述
- **实现位置**: `publish_description()` (line 1032)
- **参数**: 
  ```json
  {
    "title": "新的标题",
    "body": "新的描述"
  }
  ```

### 2. 评论相关

#### 2.1 发布评论（PR 或 Issue）
- **方法**: `POST`
- **端点**: 
  - PR: `/enterprises/{enterprise_id}/projects/{project_id}/pull_requests/{pr_number}/notes`
  - Issue: `/enterprises/{enterprise_id}/projects/{project_id}/issues/{issue_number}/comments`
- **用途**: 在 PR 或 Issue 下发布评论
- **实现位置**: `publish_comment()` (line 517)
- **参数**: 
  ```json
  {
    "body": "评论内容"
  }
  ```

#### 2.2 编辑评论
- **方法**: `PATCH`
- **端点**: 
  - PR: `/enterprises/{enterprise_id}/projects/{project_id}/pull_requests/notes/{comment_id}`
  - Issue: `/enterprises/{enterprise_id}/projects/{project_id}/issues/comments/{comment_id}`
- **用途**: 编辑已有的评论
- **实现位置**: `edit_comment()` (line 568)
- **参数**: 
  ```json
  {
    "body": "更新后的评论内容"
  }
  ```

#### 2.3 发布行内评论
- **方法**: `POST`
- **端点**: `/enterprises/{enterprise_id}/projects/{project_id}/pull_requests/{pr_number}/notes`
- **用途**: 在特定文件的特定行发布评论
- **实现位置**: `publish_inline_comment()` (line 596)
- **参数**: 
  ```json
  {
    "body": "评论内容",
    "line_code": "文件路径:L{行号}"
  }
  ```

#### 2.4 批量发布行内评论
- **方法**: `POST`（多次调用）
- **端点**: `/enterprises/{enterprise_id}/projects/{project_id}/pull_requests/{pr_number}/notes`
- **用途**: 发布多个行内评论
- **实现位置**: `publish_inline_comments()` (line 639)

#### 2.5 获取评论列表
- **方法**: `GET`
- **端点**: 
  - PR: `/enterprises/{enterprise_id}/projects/{project_id}/pull_requests/{pr_number}/notes`
  - Issue: `/enterprises/{enterprise_id}/projects/{project_id}/issues/{issue_number}/comments`
- **用途**: 获取 PR 或 Issue 下的所有评论
- **实现位置**: `get_issue_comments()` (line 926)

#### 2.6 删除评论
- **方法**: `DELETE`
- **端点**: `/enterprises/{enterprise_id}/notes/{comment_id}`
- **用途**: 删除指定的评论（PR 或 Issue 评论使用相同端点）
- **实现位置**: `remove_comment()` (line 1106)
- **⚠️ 注意**: Gitee Enterprise 删除评论的 API 路径**不包含** `project_id` 和 `pull_request_id`，与其他 Git 平台不同

### 3. Labels 相关（❌ 不支持）

**重要提示**：Gitee 企业版 API **不支持** labels 相关接口。

#### 3.1 获取 PR Labels
- **方法**: `GET`
- **端点**: `/enterprises/{enterprise_id}/projects/{project_id}/pull_requests/{pr_number}/labels`
- **状态**: ❌ **不支持** (返回 404 Not Found)
- **实现**: `get_pr_labels()` - 直接返回空列表 `[]`
- **日志**: 会输出 warning 提示

#### 3.2 添加 PR Labels
- **方法**: `POST`
- **端点**: `/enterprises/{enterprise_id}/projects/{project_id}/pull_requests/{pr_number}/labels`
- **状态**: ❌ **不支持** (返回 404 Not Found)
- **实现**: `publish_labels()` - 直接返回 `False`
- **日志**: 会输出 error 提示

**如何禁用标签功能以避免 404 错误：**

在配置文件 `settings_prod/.secrets.toml` 中设置（**默认已禁用**）：

```toml
[pr_reviewer]
enable_review_labels_effort = false      # 禁用"审查工作量"标签（默认值）
enable_review_labels_security = false    # 禁用"安全问题"标签（默认值）
```

**注意**：由于 Gitee Enterprise API 不支持 Labels 接口，默认配置已将这两个选项设为 `false`。

**影响：**
- PR 审查时不会尝试添加 "Review effort X/5" 或 "Possible security concern" 标签
- 避免不必要的 API 调用和 404 错误日志

### 4. Repository 相关（❌ 暂不支持）

以下 API **当前不支持**，已做空实现：

```bash
# ❌ 获取仓库语言统计
GET /enterprises/{enterprise_id}/projects/{project_id}/languages

# ❌ 获取文件内容
GET /enterprises/{enterprise_id}/projects/{project_id}/repository/files/{filename}
```

**原因**：Gitee 企业版可能未开放这些接口，或需要特殊权限。

**降级方案**：
- 语言统计：返回空字典 `{}`
- 文件内容：返回空字符串 `""`
- AI review 仅依赖 patch diff 文本进行分析

---

## 🔍 响应数据结构

### 1. Files 接口响应

```json
{
  "is_overflow": false,
  "total_count": 92,
  "real_count": "92",
  "data": [
    {
      "filename": ".gitignore",
      "additions": 1,
      "deletions": 0,
      "patch": {
        "diff": "@@ -22,6 +22,7 @@\n...",
        "new_file": false,
        "deleted_file": false,
        "renamed_file": false,
        "old_path": ".gitignore",
        "new_path": ".gitignore"
      }
    }
  ]
}
```

**关键点**：
- ✅ 响应是包装结构，实际数据在 `data` 字段中
- ✅ 需要提取 `response['data']` 而不是直接使用 response
- ✅ 每个文件包含完整的 patch 信息，无需额外调用 `/diff` 接口

### 2. Commits 接口响应

```json
{
  "message": "请注意，page、per_page 这两个参数已弃用，请使用 prev_id 参数来获取下一页commit列表",
  "total_count": 6,
  "has_more": false,
  "data": [
    {
      "day": "2026-05-13",
      "count": 1,
      "commits": [
        {
          "id": "ba29d00ca1e5cdeb80d2cf58d87e5fdcb5a53c28",
          "short_id": "ba29d00",
          "message": "Analysis 工程支持 2.6.0版本\n",
          "author": {...},
          "committer": {...}
        }
      ]
    },
    {
      "day": "2026-05-19",
      "count": 1,
      "commits": [...]
    }
  ]
}
```

**关键点**：
- ✅ 按日期分组的嵌套结构
- ✅ 需要扁平化处理：遍历所有 day，提取 commits 数组
- ✅ Commit 对象直接使用 `message` 字段，而非嵌套在 `commit.message` 下
- ✅ Commit 对象没有 `html_url` 字段

### 3. PR 详情响应

```json
{
  "diff_refs": {
    "head_sha": "abc123...",
    "base_sha": "def456..."
  },
  "source_branch": {
    "id": 123,
    "branch": "feature-branch",
    "project_id": 208,
    ...
  },
  "target_branch": {
    "id": 456,
    "branch": "main",
    ...
  },
  "body": "PR description...",
  ...
}
```

**关键点**：
- ✅ SHA 信息在 `diff_refs` 对象中
- ✅ `head_sha`: PR 源分支最新提交
- ✅ `base_sha`: 目标分支基准提交
- ⚠️ **重要**: `source_branch` 和 `target_branch` 是**对象**，不是字符串
- ⚠️ 分支名称需要从 `source_branch.branch` 或 `target_branch.branch` 获取
- ⚠️ 直接使用 `getattr(self.pr, 'source_branch')` 会返回 `DictToObject` 包装的对象

---

## 🔍 响应数据结构

### 不支持的功能

| 功能 | 状态 | 影响 | 降级方案 |
|------|------|------|----------|
| Repository Files API | ❌ 不支持 | base_file/head_file 为空 | 返回空字符串 |
| Languages API | ❌ 不支持 | 语言统计为空 | 返回空字典 |
| Repo Settings | ❌ 不支持 | 仓库设置为空 | 返回空字符串 |
| Incremental Review | ❌ 不支持 | 抛出 NotImplementedError | 明确提示不支持 |
| **Labels API** | ❌ **不支持** | **标签操作失败 (404)** | **返回空列表/False，可通过配置禁用** |

### 降级策略

当文件内容不可用时：
- `FilePatchInfo.base_file` = `""`
- `FilePatchInfo.head_file` = `""`
- `FilePatchInfo.patch` = 从 `/files` API 获取的 diff（**仍然可用**）

AI review 工具将主要依赖 **patch diff 文本**进行分析，这是可接受的降级方案。

---

## 🛠️ 实现要点

### 1. 数据提取模式

```python
# Files 接口
files_response = self._api_request('GET', endpoint)
if isinstance(files_response, dict) and 'data' in files_response:
    self.git_files = files_response['data']
else:
    raise ValueError("Unexpected response format")

# Commits 接口
commits_response = self._api_request('GET', endpoint)
if isinstance(commits_response, dict) and 'data' in commits_response:
    all_commits = []
    for day_group in commits_response['data']:
        if isinstance(day_group, dict) and 'commits' in day_group:
            all_commits.extend(day_group['commits'])
    self.pr_commits = all_commits

# Branch 字段提取（重要！）
# source_branch 是对象，不是字符串
source_branch_obj = getattr(self.pr, 'source_branch', None)
if source_branch_obj:
    # DictToObject 包装后，需要访问 .branch 属性获取字符串
    branch_name = source_branch_obj.branch  # 返回字符串
```

**⚠️ 注意事项**：
- Gitee API v8 的 `source_branch` 和 `target_branch` 是**对象类型**
- 通过 `DictToObject` 包装后，直接 `getattr()` 会返回 `DictToObject` 实例
- 必须访问 `.branch` 属性才能获取分支名称字符串
- 这影响 `copy.deepcopy()` 的使用，因为 `DictToObject` 不支持深拷贝

### 2. 错误处理规范

```python
# API 请求失败
if not response:
    self.logger.error("Failed to fetch data from Gitee Enterprise API")
    raise ValueError("Check your access token and network connection.")

# 响应格式异常
if unexpected_format:
    self.logger.error(f"Unexpected response format: {type(response)}, content: {response}")
    raise ValueError(f"Expected dict with 'data' key")

# 异常保护（Commits 获取）
try:
    commits_response = self._api_request('GET', endpoint)
    if not commits_response:
        self.logger.warning("Failed to fetch commits or no commits found")
        self.pr_commits = []
    else:
        # 解析逻辑...
except Exception as e:
    self.logger.error(f"Error fetching commits: {str(e)}")
    self.pr_commits = []
```

### 3. 日志级别规范

使用 `loguru` Logger：
- ✅ `self.logger.warning()` - 警告信息
- ✅ `self.logger.error()` - 错误信息
- ✅ `self.logger.info()` - 一般信息
- ❌ ~~`self.logger.warn()`~~ - **不存在此方法**

---

## ❓ 常见问题 FAQ

### Q1: 为什么评论接口使用 `/pull_requests/{id}/notes`？

**A**: 这是 Gitee 企业版 API v8 的设计规范。评论相关的接口统一使用 `/notes` 后缀。

### Q2: 如何测试 API 路径是否正确？

**A**: 使用 curl 直接测试：
```bash
curl -H "Authorization: token YOUR_TOKEN" \
     "http://your-domain/enterprises/2/projects/208/pull_requests/66/notes"
```

### Q3: Files 接口返回的数据有什么特点？

**A**: `/files` API 返回**丰富的文件信息**，包括完整的 patch、行数统计和状态标志，无需额外调用 `/diff` 接口。

### Q4: 如何处理 Commits API 按日期分组的结构？

**A**: 需要扁平化处理：

```python
if isinstance(commits_response, dict) and 'data' in commits_response:
    all_commits = []
    for day_group in commits_response['data']:
        if isinstance(day_group, dict) and 'commits' in day_group:
            all_commits.extend(day_group['commits'])
    self.pr_commits = all_commits
```

### Q5: Commit 对象没有 html_url 怎么办？

**A**: 手动构造 URL：

```python
commit_id = self.last_commit.get('id') or self.last_commit.get('short_id')
if commit_id and self.enterprise_id and self.project_id:
    return f"{self.base_url}/{self.enterprise_id}/projects/{self.project_id}/commit/{commit_id}"
```

### Q6: 为什么 `get_pr_branch()` 返回的是 `DictToObject` 而不是字符串？

**A**: 这是因为 Gitee API v8 的响应结构中，`source_branch` 是一个**对象**：

```json
{
  "source_branch": {
    "id": 123,
    "branch": "feature-branch",
    "project_id": 208
  }
}
```

当通过 `DictToObject` 包装后，`getattr(self.pr, 'source_branch')` 会返回一个新的 `DictToObject` 实例。

**正确做法**：
```python
# ✅ 正确：访问 .branch 属性获取字符串
source_branch_obj = getattr(self.pr, 'source_branch', None)
branch_name = source_branch_obj.branch  # 返回 "feature-branch" 字符串

# ❌ 错误：直接使用 getattr 返回值
branch_name = getattr(self.pr, 'source_branch', None)  # 返回 DictToObject 对象
```

**影响**：
- 如果将 `DictToObject` 对象放入 `self.vars` 字典
- 在执行 `copy.deepcopy(self.vars)` 时会失败
- 因为 `DictToObject` 类没有实现 `__deepcopy__` 方法

### Q7: 如何避免 deepcopy 错误？

**A**: 确保从 `DictToObject` 中提取出原始值（字符串、数字等）：

```python
# 在 get_pr_branch() 中
source_branch_obj = getattr(self.pr, 'source_branch', None)
return source_branch_obj.branch  # 返回字符串，而非 DictToObject

# 在 pr_reviewer.py 中
self.vars = {
    "branch": self.git_provider.get_pr_branch(),  # ✅ 现在是字符串
    ...
}

# 这样 deepcopy 就能正常工作
variables = copy.deepcopy(self.vars)  # ✅ 不会报错
```

---

## 📊 API 接口汇总表

| 功能 | HTTP 方法 | API 端点 | 实现方法 | 行号 |
|------|----------|---------|---------|------|
| 获取 PR 详情 | GET | `/enterprises/{eid}/projects/{pid}/pull_requests/{pr}` | `_fetch_pr_data()` | 302 |
| 获取 PR 文件列表 | GET | `/enterprises/{eid}/projects/{pid}/pull_requests/{pr}/files` | `_fetch_pr_data()` | 338 |
| 获取 PR Commits | GET | `/enterprises/{eid}/projects/{pid}/pull_requests/{pr}/commits` | `_fetch_pr_data()` | 367 |
| 更新 PR | PATCH | `/enterprises/{eid}/projects/{pid}/pull_requests/{pr}` | `publish_description()` | 1032 |
| 发布 PR 评论 | POST | `/enterprises/{eid}/projects/{pid}/pull_requests/{pr}/notes` | `publish_comment()` | 517 |
| 发布 Issue 评论 | POST | `/enterprises/{eid}/projects/{pid}/issues/{id}/comments` | `publish_comment()` | 517 |
| 编辑 PR 评论 | PATCH | `/enterprises/{eid}/projects/{pid}/pull_requests/notes/{id}` | `edit_comment()` | 568 |
| 编辑 Issue 评论 | PATCH | `/enterprises/{eid}/projects/{pid}/issues/comments/{id}` | `edit_comment()` | 568 |
| 发布行内评论 | POST | `/enterprises/{eid}/projects/{pid}/pull_requests/{pr}/notes` | `publish_inline_comment()` | 596 |
| 获取 PR 评论 | GET | `/enterprises/{eid}/projects/{pid}/pull_requests/{pr}/notes` | `get_issue_comments()` | 926 |
| 获取 Issue 评论 | GET | `/enterprises/{eid}/projects/{pid}/issues/{id}/comments` | `get_issue_comments()` | 926 |
| 删除评论 | DELETE | `/enterprises/{eid}/notes/{id}` | `remove_comment()` | 1106 |
| ~~获取 PR Labels~~ | ~~GET~~ | ~~`/enterprises/{eid}/projects/{pid}/pull_requests/{pr}/labels`~~ | ~~`get_pr_labels()`~~ | **❌ 不支持** |
| ~~添加 PR Labels~~ | ~~POST~~ | ~~`/enterprises/{eid}/projects/{pid}/pull_requests/{pr}/labels`~~ | ~~`publish_labels()`~~ | **❌ 不支持** |

**总计**: 15 个已实现的 API 接口（Labels 相关 2 个接口不支持）

---

## 📚 相关文件

- **Provider 实现**: `pr_agent/git_providers/gitee_enterprise_provider.py`
- **配置文件**: `pr_agent/settings_prod/.secrets.toml`
- **配置模板**: `pr_agent/settings/.secrets_template.toml`
- **注册入口**: `pr_agent/git_providers/__init__.py`
- **测试脚本**: `tests/health_test/test_litellm_connection.py`

---

## 💡 最佳实践

1. **始终检查响应格式**：Gitee 企业版 API 返回包装结构，需要提取 `data` 字段
2. **注意路径规范**：评论接口使用 `/pull_requests/{id}/notes`
3. **做好降级处理**：不支持的 API 应返回空值并记录 warning 日志
4. **统一日志级别**：使用 `warning()` 而非 `warn()`
5. **充分利用 API**：`/files` 接口已提供丰富信息，无需额外调用
6. **完善异常处理**：关键 API 调用应添加 try-except 保护

---

## 🔄 更新历史

| 日期 | 更新内容 |
|------|----------|
| 2026-05-20 | 初始创建，整合所有 Gitee 企业版 API 文档 |
| 2026-05-20 | 更新评论接口路径为 `/pull_requests/{id}/notes` |
| 2026-05-20 | 添加评论接口参数规范说明 |
| 2026-05-20 | **重要**：添加 `source_branch` 和 `target_branch` 字段类型说明（对象而非字符串） |
| 2026-05-20 | 添加 FAQ Q6/Q7：解释 DictToObject 导致的 deepcopy 问题及解决方案 |
| 2026-05-20 | **重要**：标记 Labels API 为不支持，添加配置禁用说明，更新相关文档 |
| 2026-05-20 | **重要**：修正删除评论 API 路径为 `/enterprises/{eid}/notes/{id}`（不包含 project_id） |

---

**文档维护者**: PR-Agent Team  
**反馈渠道**: 提交 Issue 或 PR
