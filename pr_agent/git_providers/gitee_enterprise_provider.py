import json
import requests
from typing import Any, Dict, List, Optional, Set, Tuple
from urllib.parse import urlparse

from pr_agent.algo.file_filter import filter_ignored
from pr_agent.algo.language_handler import is_valid_file
from pr_agent.algo.types import EDIT_TYPE
from pr_agent.algo.utils import (clip_tokens,
                                 find_line_number_of_relevant_line_in_file)
from pr_agent.config_loader import get_settings
from pr_agent.git_providers.git_provider import (MAX_FILES_ALLOWED_FULL,
                                                 FilePatchInfo, GitProvider)
from pr_agent.log import get_logger


class DictToObject:
    """
    A simple wrapper class that allows dictionary access via attribute notation.
    This makes dict objects behave like GitHub/GitLab PR objects for consistent API usage.
    """
    def __init__(self, data: dict):
        self._data = data
    
    def __getattr__(self, name: str):
        if name in self._data:
            value = self._data[name]
            # Recursively convert nested dicts to DictToObject
            if isinstance(value, dict):
                return DictToObject(value)
            elif isinstance(value, list):
                return [DictToObject(item) if isinstance(item, dict) else item for item in value]
            return value
        raise AttributeError(f"'{type(self).__name__}' object has no attribute '{name}'")
    
    def __getitem__(self, key: str):
        return self._data[key]
    
    def get(self, key: str, default=None):
        return self._data.get(key, default)
    
    def __contains__(self, key: str):
        return key in self._data
    
    def keys(self):
        return self._data.keys()
    
    def values(self):
        return self._data.values()
    
    def items(self):
        return self._data.items()
    
    def __repr__(self):
        return f"DictToObject({self._data})"


class GiteeEnterpriseProvider(GitProvider):
    """
    Gitee Enterprise (码云企业版) Git Provider implementation.
    
    Gitee Enterprise Edition uses a different API structure compared to the community edition.
    API Documentation: http://gitee.wty.cn/openapi (example)
    
    URL format: http://{base_url}/enterprises/{enterprise_id}/projects/{project_id}/pull_requests/{pr_id}
    
    Note: This is DIFFERENT from Gitee Community Edition!
    - Gitee Community: https://gitee.com/api/v5/repos/{owner}/{repo}/pulls/{number}
    - Gitee Enterprise: http://{base_url}/openapi/enterprises/{enterprise_id}/projects/{project_id}/pull_requests/{pr_id}
    """
    
    def __init__(self, url: Optional[str] = None):
        super().__init__()
        self.logger = get_logger()

        if not url:
            self.logger.error("PR URL not provided.")
            raise ValueError("PR URL not provided.")

        # Gitee Enterprise configuration - all required, no defaults
        self.base_url = get_settings().get("GITEE_ENTERPRISE.URL", None)
        if not self.base_url:
            self.logger.error("GITEE_ENTERPRISE.URL is required but not configured in settings.")
            raise ValueError("GITEE_ENTERPRISE.URL is required. Please configure it in settings_prod/.secrets.toml")
        
        self.base_url = self.base_url.rstrip("/")
        self.pr_url = ""
        self.issue_url = ""

        # Get access token - required
        self.gitee_access_token = get_settings().get("GITEE_ENTERPRISE.PERSONAL_ACCESS_TOKEN", None)
        if not self.gitee_access_token:
            self.logger.error("GITEE_ENTERPRISE.PERSONAL_ACCESS_TOKEN is required but not configured in settings.")
            raise ValueError("GITEE_ENTERPRISE.PERSONAL_ACCESS_TOKEN is required. Please configure it in settings_prod/.secrets.toml")

        # Setup API headers
        self.headers = {
            "Authorization": f"token {self.gitee_access_token}",
            "Content-Type": "application/json",
            "Accept": "application/json"
        }

        # Initialize instance variables
        self.enterprise_id = None
        self.project_id = None
        self.pr_number = None
        self.issue_number = None
        self.max_comment_chars = 65000
        self.enabled_pr = False
        self.enabled_issue = False
        self.temp_comments = []
        self.pr = None
        # git_files: PR中所有变更文件的列表，从 /files API 获取
        # 结构：[{"filename": "...", "additions": N, "deletions": N, "patch": {...}}, ...]
        # 用途：1) _load_file_contents() 加载文件内容
        #      2) _load_file_diffs() 提取文件差异
        #      3) get_diff_files() 组装完整的 FilePatchInfo 对象
        self.git_files = []
        # file_contents: 缓存的文件内容字典 {filename: content}
        # 来源：_load_file_contents() 从 PR 源分支（head commit）加载
        # 用途：get_diff_files() 中作为 head_file（修改后的文件内容）
        self.file_contents = {}
        # file_diffs: 缓存的文件差异字典 {filename: patch_text}
        # 来源：_load_file_diffs() 从 git_files 中提取 patch.diff
        # 用途：get_diff_files() 中快速查找每个文件的 patch 文本
        self.file_diffs = {}
        # diff_files: 最终的文件差异对象列表 [FilePatchInfo, ...]
        # 来源：get_diff_files() 组装，包含 base_file、head_file、patch、edit_type 等完整信息
        # 用途：对外提供统一接口，供 review/describe/improve 等 AI 工具使用
        self.diff_files = []
        self.comments_list = []
        # pr_commits: PR中所有提交的列表，用于获取完整的提交历史
        # 用途：1) get_commit_messages() 提取所有提交信息供AI分析
        #      2) 了解PR的完整变更历程
        self.pr_commits = None
        # last_commit: PR中最后一个（最新）提交对象
        # 用途：get_latest_commit_url() 获取最新提交的URL，用于在评论中链接到最新变更
        self.last_commit = None
        # PR源分支的最新提交SHA
        self.sha = None
        # base_sha: 目标分支的基准提交SHA（合并前的原始代码版本）,如果目标分支更新，这个也不会变更，除非 PR源分支重新 rebase
        self.base_sha = None

        # Parse URL and initialize
        if "pull_requests" in url or "pull_request" in url:
            self.pr_url = url
            self._set_repo_and_owner_from_pr()
            self.enabled_pr = True
            self._fetch_pr_data()
        else:
            self.logger.error(f"Invalid Gitee Enterprise URL: {url}")
            raise ValueError(f"Invalid Gitee Enterprise URL: {url}")

    def _api_request(self, method: str, endpoint: str, **kwargs) -> Optional[Any]:
        """
        Make HTTP request to Gitee Enterprise OpenAPI
        
        Args:
            method: HTTP method (GET, POST, PATCH, DELETE, PUT)
            endpoint: API endpoint (without base URL)
            **kwargs: Additional arguments for requests
            
        Returns:
            Response data or None if failed
        """
        url = f"{self.base_url}{endpoint}"
        
        # Add access_token as query parameter for GET requests (Gitee Enterprise style)
        if method.upper() == 'GET' and self.gitee_access_token:
            # Check if params already exist
            params = kwargs.get('params', {})
            params['access_token'] = self.gitee_access_token
            kwargs['params'] = params
        
        try:
            response = requests.request(
                method=method,
                url=url,
                headers=self.headers,
                timeout=30,
                **kwargs
            )
            
            if response.status_code == 204:
                return True  # No content success
            
            # Check if response is HTML (error page)
            content_type = response.headers.get('Content-Type', '')
            if response.status_code != 200 and 'text/html' in content_type:
                self.logger.error(f"API returned HTML instead of JSON. Status: {response.status_code}")
                self.logger.error(f"URL: {url}")
                self.logger.error(f"Response preview: {response.text[:200]}")
                return None
            
            response.raise_for_status()
            
            # Try to parse JSON response
            try:
                return response.json()
            except json.JSONDecodeError:
                self.logger.warning(f"Failed to parse JSON response. Content type: {content_type}")
                self.logger.warning(f"Response preview: {response.text[:200]}")
                return response.text
                
        except requests.exceptions.RequestException as e:
            self.logger.error(f"Gitee Enterprise API request failed: {method} {url}, Error: {str(e)}")
            if hasattr(e, 'response') and e.response is not None:
                self.logger.error(f"Response status: {e.response.status_code}, body: {e.response.text[:200]}")
            return None

    def _parse_pr_url(self, pr_url: str) -> Tuple[str, str, int]:
        """
        Parse Gitee Enterprise PR URL to extract enterprise_id, project_id, and PR number
        
        Gitee Enterprise PR URL format: 
        http://{base_url}/enterprises/{enterprise_id}/projects/{project_id}/pull_requests/{number}, number is the iid.
        
        TODO: support http://gitee.wty.cn/stc/repos/stc/pi_data_platform/pulls/5, which 5 is iid
        """
        parsed_url = urlparse(pr_url)
        path_parts = parsed_url.path.strip('/').split('/')
        
        # Find the positions of enterprises, projects, and pull_requests in the path
        try:
            enterprises_idx = path_parts.index('enterprises')
            projects_idx = path_parts.index('projects')
            pull_requests_idx = next(i for i, part in enumerate(path_parts) if part in ['pull_requests', 'pull_request'])
            
            enterprise_id = path_parts[enterprises_idx + 1]
            project_id = path_parts[projects_idx + 1]
            pr_number = int(path_parts[pull_requests_idx + 1])
            
        except (ValueError, IndexError) as e:
            raise ValueError(f"The provided URL does not appear to be a Gitee Enterprise PR URL: {pr_url}") from e

        return enterprise_id, project_id, pr_number


    def _set_repo_and_owner_from_pr(self):
        """Extract enterprise_id, project_id from the PR URL"""
        try:
            enterprise_id, project_id, pr_number = self._parse_pr_url(self.pr_url)
            self.enterprise_id = enterprise_id
            self.project_id = project_id
            self.pr_number = pr_number
            self.logger.info(f"Gitee Enterprise PR - Enterprise ID: {self.enterprise_id}, Project ID: {self.project_id}, PR Number: {self.pr_number}")
        except ValueError as e:
            self.logger.error(f"Error parsing PR URL: {str(e)}")
            raise
        except Exception as e:
            self.logger.error(f"Unexpected error: {str(e)}")
            raise

    def _fetch_pr_data(self):
        """Fetch PR data and related information from Gitee Enterprise API v8"""
        if not self.pr_number:
            return

        # Fetch PR details, use iid
        pr_data = self._api_request('GET', f"/enterprises/{self.enterprise_id}/projects/{self.project_id}/pull_requests/{self.pr_number}", params={"pr_qt": "iid"})
        if not pr_data:
            self.logger.error(f"Failed to fetch PR #{self.pr_number}")
            raise ValueError(f"Failed to fetch PR #{self.pr_number}. Check your access token and URL.")
        
        # Wrap the dictionary in DictToObject for attribute-style access
        self.pr = DictToObject(pr_data)
        
        # Extract SHA from diff_refs (Gitee API v8 structure)
        # diff_refs contains both head_sha (PR source branch) and base_sha (target branch)
        if not hasattr(self.pr, 'diff_refs') or not self.pr.diff_refs:
            self.logger.error("PR data missing 'diff_refs' field")
            self.logger.error(f"Available fields: {list(pr_data.keys())}")
            raise ValueError("PR data missing 'diff_refs' field. This may indicate an API version mismatch.")
        
        diff_refs = self.pr.diff_refs
        # sha (head_sha): PR源分支的最新提交SHA，代表修改后的代码版本
        self.sha = diff_refs.get('head_sha')
        # base_sha: 目标分支的基准提交SHA，代表合并前的原始代码版本
        self.base_sha = diff_refs.get('base_sha')
    
        if not self.sha:
            self.logger.error("Cannot extract head_sha from diff_refs")
            self.logger.error(f"diff_refs structure: {diff_refs}")
            raise ValueError("Cannot extract head_sha from diff_refs")
        
        self.logger.info(f"PR SHA: {self.sha}, Base SHA: {self.base_sha}")
        
        # Fetch changed files
        # Gitee Enterprise API v8 returns wrapped response structure:
        # {
        #   "is_overflow": false,
        #   "total_count": 92,
        #   "real_count": "92",
        #   "data": [file1, file2, ...]
        # }
        files_response = self._api_request(
            'GET', 
            f"/enterprises/{self.enterprise_id}/projects/{self.project_id}/pull_requests/{self.pr_number}/files",
            params={"pr_qt": "iid"}
        )
        
        if not files_response:
            self.logger.error("Failed to fetch changed files from Gitee Enterprise API")
            raise ValueError("Failed to fetch changed files. Check your access token and network connection.")
        
        # Extract data array from response (similar to commits handling)
        if isinstance(files_response, dict) and 'data' in files_response:
            self.git_files = files_response['data']
        else:
            self.logger.error(f"Unexpected files response format: {type(files_response)}, content: {files_response}")
            raise ValueError(f"Unexpected files response format: {type(files_response)}. Expected dict with 'data' key.")
        
        # Filter ignored files
        if self.git_files:
            self.git_files = filter_ignored(self.git_files, platform="gitee")
        
        # Fetch commits: 获取PR中的所有提交记录
        # Gitee Enterprise API v8 返回按日期分组的数据结构:
        # {
        #   "data": [
        #     {"day": "2026-05-13", "commits": [commit1, commit2, ...]},
        #     {"day": "2026-05-14", "commits": [...]},
        #     ...
        #   ]
        # }
        commits_response = self._api_request(
            'GET',
            f"/enterprises/{self.enterprise_id}/projects/{self.project_id}/pull_requests/{self.pr_number}/commits",
            params={"pr_qt": "iid"} 
        )
        
        if not commits_response:
            self.logger.error("Failed to fetch commits from Gitee Enterprise API")
            raise ValueError("Failed to fetch commits. Check your access token and network connection.")
        
        # Parse the grouped commits data structure
        # Extract all commits from the nested "data" array
        if isinstance(commits_response, dict) and 'data' in commits_response:
            # Flatten commits from all days into a single list
            all_commits = []
            for day_group in commits_response['data']:
                if isinstance(day_group, dict) and 'commits' in day_group:
                    all_commits.extend(day_group['commits'])
            self.pr_commits = all_commits
        else:
            self.logger.error(f"Unexpected commits response format: {type(commits_response)}")
            raise ValueError(f"Unexpected commits response format: {type(commits_response)}. Expected dict with 'data' key or list.")
        
        # Extract last_commit: 取列表最后一个元素作为最新提交
        # 用于 get_latest_commit_url() 方法生成最新提交的链接
        if self.pr_commits:
            self.last_commit = self.pr_commits[-1]
        else:
            self.logger.error("Commits list is empty after parsing")
            raise ValueError("No commits found in the PR")
        
        # Load file contents and diffs
        self._load_file_contents()
        self._load_file_diffs()

    def _load_file_contents(self):
        """
        Load file contents for all changed files from the PR's head commit.
        
        NOTE: This API endpoint is not currently supported by Gitee Enterprise.
        File contents will be loaded on-demand when needed.
        """
        # TODO: Implement when Gitee Enterprise supports repository files API
        # For now, file contents will be empty and loaded on-demand if needed
        self.logger.warning("File contents loading is not supported, skipping")
        pass

    def _load_file_diffs(self):
        """
        Load file diffs for all changed files.
        
        Note: Gitee Enterprise /files API returns rich file information including:
        - patch.diff: The unified diff text
        - additions/deletions: Line counts
        - status: File status (added/modified/deleted)
        - patch.new_file/renamed_file/deleted_file: Boolean flags
        
        We extract patch information from git_files and store in self.file_diffs.
        """
        # git_files already contains full file info from _fetch_pr_data()
        # Extract patch information from each file
        for file_info in self.git_files:
            if not isinstance(file_info, dict):
                continue
            
            filename = file_info.get('filename')
            if not filename:
                continue
            
            # Extract patch from the nested 'patch' object
            # Format: file_info['patch']['diff']
            patch_obj = file_info.get('patch', {})
            if isinstance(patch_obj, dict):
                patch_text = patch_obj.get('diff', '')
                self.file_diffs[filename] = patch_text
            else:
                # Fallback: empty patch when patch object is missing or invalid
                self.logger.warning(f"File '{filename}' has no valid patch object")
                self.file_diffs[filename] = ""
        
        self.logger.info(f"Loaded diffs for {len(self.file_diffs)} files")

    def is_supported(self, capability: str) -> bool:
        """Check if the capability is supported"""
        # Gitee Enterprise does not support incremental review
        if capability in ['incremental_review']:
            return False
        # Gitee Enterprise supports most other capabilities
        return True

    def get_incremental_commits(self, is_incremental):
        """
        Gitee Enterprise does not support incremental review.
        Raises an exception if incremental review is requested.
        """
        if is_incremental and is_incremental.is_incremental:
            error_msg = "Incremental review (-i flag) is not supported for Gitee Enterprise"
            self.logger.error(error_msg)
            raise NotImplementedError(error_msg)

    def get_pr_url(self) -> str:
        """Get PR URL"""
        return self.pr_url

    def get_issue_url(self) -> str:
        """Get Issue URL"""
        return self.issue_url

    def get_latest_commit_url(self) -> str:
        """
        Get latest commit URL
        
        Returns the HTML URL of the most recent commit in the PR.
        Used to provide clickable links in comments pointing to the latest changes.
        
        Example usage: In PR description updates, shows "updated to latest commit (URL)"
        
        Note: Gitee Enterprise commit objects have 'id' field but may not have 'html_url'.
        We construct the URL manually if needed.
        """
        if not self.last_commit:
            return ""
        
        # Try to get html_url directly (if available)
        if self.last_commit.get('html_url'):
            return self.last_commit['html_url']
        
        # Fallback: Construct URL from commit id
        # Format: {base_url}/{enterprise_id}/projects/{project_id}/commit/{commit_id}
        commit_id = self.last_commit.get('id') or self.last_commit.get('short_id')
        if commit_id and self.enterprise_id and self.project_id:
            return f"{self.base_url}/{self.enterprise_id}/projects/{self.project_id}/commit/{commit_id}"
        
        return ""

    def get_comment_url(self, comment) -> str:
        """Get comment URL"""
        if isinstance(comment, dict):
            return comment.get('html_url', '')
        return getattr(comment, 'html_url', '')

    def publish_persistent_comment(self, pr_comment: str,
                                   initial_header: str,
                                   update_header: bool = True,
                                   name='review',
                                   final_update_message=True):
        """Publish persistent comment with update tracking"""
        self.publish_persistent_comment_full(
            pr_comment, initial_header, update_header, name, final_update_message
        )

    def publish_comment(self, comment: str, is_temporary: bool = False) -> Optional[Dict]:
        """
        Publish a comment to the PR or Issue
        
        API: POST /enterprises/{enterprise_id}/projects/{project_id}/pull_requests/{number}/notes
        """
        if is_temporary and not get_settings().config.publish_output_progress:
            self.logger.debug(f"Skipping publish_comment for temporary comment")
            return None

        endpoint = f"/enterprises/{self.enterprise_id}/projects/{self.project_id}/pull_requests/{self.pr_number}/notes"
      

        comment = self.limit_output_characters(comment, self.max_comment_chars)
        
        # Gitee Enterprise API parameters for PR comments:
        # - body*: Comment content (required)
        # - line_code: Code line marker (optional, for inline comments)
        # - diff_position_id: Diff position group ID (optional)
        # - reply_id: Parent comment ID for replies (optional)
        # Note: access_token, enterprise_id, project_id, pull_request_id are in URL path
        payload = {"body": comment}
        
        response = self._api_request('POST', endpoint, json=payload, params={"pr_qt": "iid"} )
        
        if not response:
            self.logger.error("Failed to publish comment")
            return None

        if is_temporary:
            # Store comment_id for later removal
            self.temp_comments.append({
                "comment": comment,
                "response": response,
                "comment_id": response.get('id')  # IMPORTANT: Must include comment_id for deletion
            })
            self.logger.info(f"Temporary comment added to temp_comments list (id={response.get('id')})")

        comment_obj = {
            "is_temporary": is_temporary,
            "comment": comment,
            "comment_id": response.get('id'),
            "html_url": response.get('html_url', '')
        }
        self.comments_list.append(comment_obj)
        self.logger.info(f"Comment published successfully: {comment_obj['html_url']}")
        return comment_obj

    def edit_comment(self, comment, body: str) -> Optional[bool]:
        """
        Edit an existing comment
        
        API: PATCH /openapi/enterprises/{enterprise_id}/projects/{project_id}/pull_requests/notes/{id}
        """
        body = self.limit_output_characters(body, self.max_comment_chars)
        
        try:
            comment_id = comment.get("comment_id") if isinstance(comment, dict) else comment.get('id')
            if not comment_id:
                self.logger.error("Comment ID not found")
                return None
            
            # Determine endpoint based on comment type
            endpoint = f"/enterprises/{self.enterprise_id}/projects/{self.project_id}/pull_requests/notes/{comment_id}"
            
            response = self._api_request('PATCH', endpoint, json={"body": body})
            return response is not None
            
        except Exception as e:
            self.logger.error(f"Error editing comment: {e}")
            return None

    def publish_inline_comment(self, body: str, relevant_file: str, 
                              relevant_line_in_file: str, original_suggestion=None):
        """
        Publish an inline comment on a specific line
        
        Note: Gitee inline comments work differently than GitHub/Gitea
        We need to find the position and create a review comment
        """
        body = self.limit_output_characters(body, self.max_comment_chars)
        
        # Find line position in diff
        position, absolute_position = find_line_number_of_relevant_line_in_file(
            self.diff_files,
            relevant_file.strip('`'),
            relevant_line_in_file,
        )
        
        if position == -1:
            self.logger.info(f"Could not find position for {relevant_file} {relevant_line_in_file}")
            # Fallback to regular comment
            return self.publish_comment(f"**File:** `{relevant_file}`\n\n{body}")

        # Create inline comment payload
        # Gitee Enterprise API parameters for inline comments:
        # - body*: Comment content (required)
        # - line_code: Code line marker (optional, format: "file_path:L{line_number}")
        # - diff_position_id: Diff position group ID (optional)
        # Note: access_token, enterprise_id, project_id, pull_request_id are in URL path
        payload = {
            "body": body,
            "line_code": f"{relevant_file.strip()}:L{position}"  # Gitee line_code format
        }
                    
        endpoint = f"/enterprises/{self.enterprise_id}/projects/{self.project_id}/pull_requests/{self.pr_number}/notes"
        response = self._api_request('POST', endpoint, json=payload, params={"pr_qt": "iid"})
        
        if response:
            self.logger.info(f"Inline comment published at {relevant_file}:{position}")
            return response
        else:
            self.logger.error("Failed to publish inline comment")
            return None

    def publish_inline_comments(self, comments: List[Dict[str, Any]]):
        """
        Publish multiple inline comments
        
        Note: Gitee doesn't support batch inline comments, so we post them one by one
        """
        results = []
        for comment_data in comments:
            body = comment_data.get('body', '')
            path = comment_data.get('path', '')
            position = comment_data.get('position')
            line = comment_data.get('line')
            
            if not body or not path:
                self.logger.warning("Skipping invalid inline comment")
                continue
            
            # Gitee Enterprise API parameters for inline comments:
            # - body*: Comment content (required)
            # - line_code: Code line marker (optional, format: "file_path:L{line_number}")
            payload = {
                "body": body,
            }
            
            # Add line_code if position/line info is available
            if position is not None and path:
                payload["line_code"] = f"{path}:L{position}"
            elif line is not None and path:
                payload["line_code"] = f"{path}:L{line}"
            
            endpoint = f"/enterprises/{self.enterprise_id}/projects/{self.project_id}/pull_requests/{self.pr_number}/notes"
            response = self._api_request('POST', endpoint, json=payload, params={"pr_qt": "iid"})
            
            if response:
                results.append(response)
            else:
                self.logger.error(f"Failed to publish inline comment for {path}")
        
        return results

    def publish_code_suggestions(self, suggestions: List[Dict[str, Any]]) -> bool:
        """
        Publish code suggestions as inline comments
        
        Each suggestion becomes an inline comment with the suggestion body
        """
        if not suggestions:
            return False
        
        success_count = 0
        for suggestion in suggestions:
            body = suggestion.get("body", "")
            if not body:
                self.logger.warning("Skipping suggestion without body")
                continue

            relevant_file = suggestion.get("relevant_file", "")
            relevant_lines_start = suggestion.get("relevant_lines_start", 0)
            
            if not relevant_file:
                self.logger.warning("Skipping suggestion without file")
                continue
            
            # Format the suggestion
            suggestion_content = suggestion.get("original_suggestion", {}).get("suggestion_content", "")
            if suggestion_content:
                formatted_body = f"**Suggestion:** {suggestion_content}\n\n---\n\n{body}"
            else:
                formatted_body = body
            
            # Publish as inline comment
            result = self.publish_inline_comment(
                body=formatted_body,
                relevant_file=relevant_file,
                relevant_line_in_file=str(relevant_lines_start),
                original_suggestion=suggestion
            )
            
            if result:
                success_count += 1
        
        self.logger.info(f"Published {success_count}/{len(suggestions)} code suggestions")
        return success_count > 0

    def add_eyes_reaction(self, issue_comment_id: int, disable_eyes: bool = False) -> Optional[int]:
        """
        Add eyes reaction to a comment
        
        Note: Gitee doesn't natively support reactions like GitHub.
        This is a no-op for now.
        """
        if disable_eyes:
            return None
        
        self.logger.warning("Gitee Enterprise does not support comment reactions")
        return None

    def remove_reaction(self, issue_comment_id: int, reaction_id: int) -> bool:
        """
        Remove reaction from a comment
        
        Note: Gitee doesn't natively support reactions
        """
        self.logger.warning("Gitee Enterprise does not support comment reactions")
        return False

    def get_commit_messages(self) -> str:
        """
        Get commit messages for the PR
        
        API: GET /openapi/enterprises/{enterprise_id}/projects/{project_id}/pull_requests/{number}/commits
        
        Purpose:
        - Extracts all commit messages from the PR's commit history
        - Provides context to AI models about the evolution of changes
        - Used in tools like pr_reviewer, pr_description, pr_code_suggestions, etc.
        
        Returns:
            String containing all commit messages joined by newlines,
            clipped to MAX_COMMITS_TOKENS if configured
        """
        max_tokens = get_settings().get("CONFIG.MAX_COMMITS_TOKENS", None)
        
        if not self.pr_commits:
            self.logger.warning("No commits found")
            return ""

        try:
            commit_messages = []
            # Iterate through all commits in the PR
            # Gitee Enterprise API v8 returns commits with direct 'message' field
            # (not nested under 'commit' like GitHub/GitLab)
            for commit in self.pr_commits:
                if not commit:
                    continue
                
                # Try to get message from different possible locations
                # Format 1: Direct 'message' field (Gitee Enterprise v8)
                message = commit.get('message', '')
                
                if message:
                    commit_messages.append(message)
                else:
                    # Log warning when a commit has no message
                    commit_id = commit.get('id') or commit.get('short_id', 'unknown')
                    self.logger.warning(f"Commit {commit_id} has no message")

            if not commit_messages:
                self.logger.warning("No commit messages found")
                return ""

            # Join all commit messages with newlines
            commit_message = "\n".join(commit_messages)
            
            # Clip to token limit if configured
            if max_tokens:
                commit_message = clip_tokens(commit_message, max_tokens)

            return commit_message
            
        except Exception as e:
            self.logger.error(f"Error processing commit messages: {str(e)}")
            return ""

    def _get_file_content_from_base(self, filename: str) -> str:
        """
        Get file content from the base branch (target branch before PR merge).
        
        NOTE: This API endpoint is not currently supported by Gitee Enterprise.
        Returns empty string.
        """
        # TODO: Implement when Gitee Enterprise supports repository files API
        self.logger.warning(f"Base file content loading is not supported for {filename}, returning empty string")
        return ""

    def get_diff_files(self) -> List[FilePatchInfo]:
        """
        Get files that were modified in the PR
        
        Returns list of FilePatchInfo objects
        """
        if self.diff_files:
            return self.diff_files

        invalid_files_names = []
        counter_valid = 0
        diff_files = []
        
        for file_info in self.git_files:
            filename = file_info.get('filename')
            if not filename:
                continue

            if not is_valid_file(filename):
                invalid_files_names.append(filename)
                continue

            counter_valid += 1
            avoid_load = False
            patch = self.file_diffs.get(filename, "")
            head_file = ""
            base_file = ""

            # Limit full file loading for large PRs to avoid excessive API calls and memory usage
            if counter_valid >= MAX_FILES_ALLOWED_FULL and patch:
                avoid_load = True
                if counter_valid == MAX_FILES_ALLOWED_FULL:
                    self.logger.info("Too many files in PR, will avoid loading full content for rest of files")

            # Get head file content (NEW version after PR changes)
            # This is the modified file from the PR's source branch
            if avoid_load:
                head_file = ""
            else:
                # Content pre-loaded from PR head commit (self.sha) in _load_file_contents()
                head_file = self.file_contents.get(filename, "")

            # Get base file content (ORIGINAL version before PR changes)
            # This is the file from the target branch before the PR was merged
            # NOTE: base_file and head_file are DIFFERENT versions of the same file,
            #       NOT base64 encoded versions of each other!
            if avoid_load:
                base_file = ""
            else:
                # Fetch from base branch (self.base_sha) via API
                base_file = self._get_file_content_from_base(filename)

            # Get line counts
            num_plus_lines = file_info.get('additions', 0)
            num_minus_lines = file_info.get('deletions', 0)
            
            # Get patch object for status detection
            patch_obj = file_info.get('patch', {})

            # Determine edit type based on patch object fields
            edit_type = EDIT_TYPE.MODIFIED
            if patch_obj.get('new_file'):
                edit_type = EDIT_TYPE.ADDED
            elif patch_obj.get('deleted_file'):
                edit_type = EDIT_TYPE.DELETED
            elif patch_obj.get('renamed_file'):
                edit_type = EDIT_TYPE.RENAMED
            
            old_path = patch_obj.get('old_path')
            old_filename = None if old_path == filename else old_path

            # Create FilePatchInfo object with both file versions
            # base_file: Original content from target branch (before PR changes)
            # head_file: Modified content from source branch (after PR changes)
            # patch: The diff between base_file and head_file
            file_patch_info = FilePatchInfo(
                base_file=base_file,      # ORIGINAL version (target branch)
                head_file=head_file,      # NEW version (source branch)
                patch=patch,              # Diff between the two versions
                filename=filename,
                num_minus_lines=num_minus_lines,
                num_plus_lines=num_plus_lines,
                edit_type=edit_type,
                old_filename=old_filename
            )
            diff_files.append(file_patch_info)

        if invalid_files_names:
            self.logger.info(f"Filtered out files with invalid extensions: {invalid_files_names}")

        self.diff_files = diff_files
        return diff_files

    def get_line_link(self, relevant_file: str, relevant_line_start: int, 
                     relevant_line_end: int = None) -> str:
        """Generate link to specific line(s) in a file"""
        branch = self.get_pr_branch()
        
        if relevant_line_start == -1:
            link = f"{self.base_url}/{self.enterprise_id}/projects/{self.project_id}/blob/{branch}/{relevant_file}"
        elif relevant_line_end:
            link = f"{self.base_url}/{self.enterprise_id}/projects/{self.project_id}/blob/{branch}/{relevant_file}#L{relevant_line_start}-L{relevant_line_end}"
        else:
            link = f"{self.base_url}/{self.enterprise_id}/projects/{self.project_id}/blob/{branch}/{relevant_file}#L{relevant_line_start}"

        return link

    def get_pr_id(self) -> str:
        """Get PR identifier"""
        try:
            return f"enterprise/{self.enterprise_id}/project/{self.project_id}#{self.pr_number}"
        except:
            return ""

    def get_files(self) -> List[str]:
        """Get list of files changed in the PR"""
        return [file_info.get('filename', '') for file_info in self.git_files if file_info.get('filename')]

    def get_num_of_files(self) -> int:
        """Get number of files changed"""
        return len(self.git_files)

    def get_issue_comments(self) -> List[Dict[str, Any]]:
        """
        Get all comments on the PR or Issue
        
        API: GET /enterprises/{enterprise_id}/projects/{project_id}/pull_requests/{number}/notes
        """
        
        endpoint = f"/enterprises/{self.enterprise_id}/projects/{self.project_id}/pull_requests/{self.pr_number}/notes"
      

        comments = self._api_request('GET', endpoint, params={"pr_qt": "iid"})
        
        if not comments:
            self.logger.warning("Failed to get comments or no comments found")
            return []

        # Convert dict list to DictToObject list for consistent API usage
        comments_list = comments if isinstance(comments, list) else []
        return [DictToObject(comment) for comment in comments_list]

    def get_languages(self):
        """
        Get programming languages used in the repository
        
        NOTE: This API endpoint is not currently supported by Gitee Enterprise.
        Returns empty dictionary.
        """
        endpoint = f"/enterprises/{self.enterprise_id}/projects/{self.project_id}/summary"
      
        result = self._api_request('GET', endpoint)
        
        # Gitee API returns {"languages": [{"language": "...", "percent": ..., ...}, ...]}
        languages_list = result.get('languages', [])
        
        if not isinstance(languages_list, list):
            return {}
        
        # Extract language names and percentages from the list
        # Return format matches GitHub/GitLab: {language: percentage}
        return {lang_info['language']: lang_info['percent'] 
                for lang_info in languages_list 
                if lang_info.get('language') and 'percent' in lang_info}

    def get_pr_branch(self) -> str:
        """Get the source branch name of the PR (Gitee API v8 structure)"""
        if not self.pr:
            self.logger.error("PR data not loaded")
            raise ValueError("PR data not loaded. Call _fetch_pr_data() first.")

        # Gitee API v8: source branch is 'source_branch' dict
        source_branch = getattr(self.pr, 'source_branch', None)
        
        if not source_branch:
            self.logger.error("Cannot determine PR source branch")
            raise ValueError("Cannot determine PR source branch from PR data")
        
        return source_branch.branch

    def get_pr_description_full(self) -> str:
        """Get full PR description"""
        if not self.pr:
            self.logger.error("PR data not loaded")
            return ""

        return self.pr.body or ""

    def get_pr_labels(self, update: bool = False) -> List[str]:
        """
        Get labels assigned to the PR
        
        ⚠️ IMPORTANT: Gitee Enterprise API does NOT support labels endpoints.
        This method will always return an empty list and log a warning.
        
        API: GET /enterprises/{enterprise_id}/projects/{project_id}/pull_requests/{number}/labels
        Status: ❌ Not supported (returns 404)
        
        To disable label-related operations and avoid 404 errors, set in configuration:
        [pr_reviewer]
        enable_review_labels_effort = false
        enable_review_labels_security = false
        """
        self.logger.warning(
            "Gitee Enterprise API does not support labels. "
            "To suppress this warning, disable label features in config: "
            "enable_review_labels_effort=false, enable_review_labels_security=false"
        )
        return []

    def get_repo_settings(self) -> str:
        """
        Get repository settings file content
        
        NOTE: This feature is not currently supported by Gitee Enterprise.
        Returns empty string.
        """
        # TODO: Implement when Gitee Enterprise supports repo settings
        self.logger.warning("Repo settings API is not supported, returning empty string")
        return ""

    def get_user_id(self) -> str:
        """Get the authenticated user's ID"""
        if self.pr and hasattr(self.pr, 'user'):
            return str(self.pr.user.id) if hasattr(self.pr.user, 'id') else ""
        return ""

    def get_git_repo_url(self, issues_or_pr_url: str) -> str:
        """Get Git clone URL for the repository"""
        return f"{self.base_url}/{self.enterprise_id}/projects/{self.project_id}.git"

    def publish_description(self, pr_title: str, pr_body: str) -> Optional[bool]:
        """
        Update PR title and description
        
        API: PATCH /openapi/enterprises/{enterprise_id}/projects/{project_id}/pull_requests/{number}
        """
        
        payload = {
            "title": pr_title,
            "body": pr_body
        }
        
        endpoint = f"/enterprises/{self.enterprise_id}/projects/{self.project_id}/pull_requests/{self.pr_number}"
        response = self._api_request('PATCH', endpoint, json=payload, params={"pr_qt": "iid"})
        
        if not response:
            self.logger.error("Failed to update PR description")
            return False

        # Refresh PR data
        if self.enabled_pr:
            self.pr = DictToObject(self._api_request('GET', endpoint))
        
        self.logger.info("PR description updated successfully")
        return True

    def publish_labels(self, labels: List[str]) -> Optional[bool]:
        """
        Add labels to the PR
        
        ⚠️ IMPORTANT: Gitee Enterprise API does NOT support labels endpoints.
        This method will always return False and log an error.
        
        API: POST /enterprises/{enterprise_id}/projects/{project_id}/pull_requests/{number}/labels
        Status: ❌ Not supported (returns 404)
        
        To disable label-related operations and avoid 404 errors, set in configuration:
        [pr_reviewer]
        enable_review_labels_effort = false
        enable_review_labels_security = false
        """
        self.logger.error(
            "Gitee Enterprise API does not support adding labels. "
            "This operation will fail with 404 error. "
            "To suppress this error, disable label features in config: "
            "enable_review_labels_effort=false, enable_review_labels_security=false"
        )
        return False

    def remove_comment(self, comment) -> None:
        """
        Remove a specific comment
        
        API: DELETE /enterprises/{enterprise_id}/notes/{id}
        Note: Gitee Enterprise uses a simplified endpoint without project_id and pull_request_id
        """
        if not comment:
            self.logger.warning("remove_comment called with empty comment")
            return

        try:
            comment_id = comment.get("comment_id") if isinstance(comment, dict) else comment.get('id')
            if not comment_id:
                self.logger.error(f"Comment ID not found in comment: {comment}")
                return
            
            # Gitee Enterprise delete comment endpoint (simplified path)
            endpoint = f"/enterprises/{self.enterprise_id}/notes/{comment_id}"
            
            self.logger.info(f"Deleting comment {comment_id} via endpoint: {endpoint}")
            result = self._api_request('DELETE', endpoint)
            self.logger.info(f"DELETE API returned: {result} (type: {type(result)})")
            
            if result:
                # Remove from local list
                self.comments_list = [c for c in self.comments_list if c.get('comment_id') != comment_id]
                self.logger.info(f"Comment {comment_id} removed successfully")
            else:
                self.logger.error(f"Failed to remove comment {comment_id} - API returned None or False")
                
        except Exception as e:
            self.logger.error(f"Error removing comment: {e}", exc_info=True)
            raise

    def remove_initial_comment(self) -> None:
        """Remove all temporary comments"""
        self.logger.info(f"Removing {len(self.temp_comments)} temporary comment(s)")
        
        for i, comment in enumerate(self.temp_comments):
            try:
                self.logger.info(f"Attempting to remove temp comment #{i+1}: {comment}")
                if isinstance(comment, dict) and comment.get('comment_id'):
                    self.remove_comment(comment)
                else:
                    self.logger.warning(f"Temp comment #{i+1} has no comment_id: {comment}")
            except Exception as e:
                self.logger.error(f"Error removing temporary comment #{i+1}: {e}", exc_info=True)
        
        self.temp_comments.clear()
        self.logger.info("All temporary comments removed")

    def _prepare_clone_url_with_token(self, repo_url_to_clone: str) -> Optional[str]:
        """
        Prepare clone URL with authentication token
        
        Gitee supports token in URL: https://{token}@gitee.com/owner/repo.git
        """
        if not self.gitee_access_token:
            self.logger.warning("No access token available for cloning")
            return repo_url_to_clone
        
        # Insert token into URL
        if repo_url_to_clone.startswith('https://'):
            # https://gitee.com/owner/repo.git -> https://token@gitee.com/owner/repo.git
            return repo_url_to_clone.replace('https://', f'https://{self.gitee_access_token}@')
        
        return repo_url_to_clone
