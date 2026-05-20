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


class GiteeProvider(GitProvider):
    """
    Gitee (码云) Git Provider implementation.
    
    Gitee is a Chinese code hosting platform similar to GitHub.
    API Documentation: https://gitee.com/api/v5/swagger
    
    Note: Gitee is DIFFERENT from Gitea!
    - Gitee uses API v5: https://gitee.com/api/v5
    - Gitea uses API v1: http://your-domain/api/v1
    """
    
    def __init__(self, url: Optional[str] = None):
        super().__init__()
        self.logger = get_logger()

        if not url:
            self.logger.error("PR URL not provided.")
            raise ValueError("PR URL not provided.")

        # Gitee configuration
        self.base_url = get_settings().get("GITEE.URL", "https://gitee.com").rstrip("/")
        self.pr_url = ""

        # Get access token
        self.gitee_access_token = get_settings().get("GITEE.PERSONAL_ACCESS_TOKEN", None)
        if not self.gitee_access_token:
            self.logger.error("Gitee access token not found in settings.")
            raise ValueError("Gitee access token not found in settings.")

        # Setup API headers
        self.headers = {
            "Authorization": f"token {self.gitee_access_token}",
            "Content-Type": "application/json",
            "Accept": "application/json"
        }

        # Repository settings file path
        self.repo_settings = get_settings().get("GITEE.REPO_SETTING", None)

        # Initialize instance variables
        self.owner = None
        self.repo = None
        self.pr_number = None
        self.max_comment_chars = 65000
        self.enabled_pr = False
        self.temp_comments = []
        self.pr = None
        self.git_files = []
        self.file_contents = {}
        self.file_diffs = {}
        self.sha = None
        self.diff_files = []
        self.comments_list = []
        self.pr_commits = None
        self.last_commit = None
        self.base_sha = None

        # Parse URL and initialize - Gitee Provider only supports Pull Requests
        if "pulls" in url or "pull" in url:
            self.pr_url = url
            self._set_repo_and_owner_from_pr()
            self.enabled_pr = True
            self._fetch_pr_data()
        elif "issues" in url:
            # Gitee Provider does not support Issues
            error_msg = "Gitee Provider does not support Issue URLs. Please use a Pull Request URL instead."
            self.logger.error(error_msg)
            raise NotImplementedError(error_msg)
        else:
            self.logger.error(f"Invalid Gitee URL: {url}")
            raise ValueError(f"Invalid Gitee URL: {url}")

    def _api_request(self, method: str, endpoint: str, **kwargs) -> Optional[Any]:
        """
        Make HTTP request to Gitee API v5
        
        Args:
            method: HTTP method (GET, POST, PATCH, DELETE, PUT)
            endpoint: API endpoint (without base URL)
            **kwargs: Additional arguments for requests
            
        Returns:
            Response data or None if failed
        """
        url = f"{self.base_url}/api/v5{endpoint}"
        
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
            
            response.raise_for_status()
            
            # Try to parse JSON response
            try:
                return response.json()
            except json.JSONDecodeError:
                return response.text
                
        except requests.exceptions.RequestException as e:
            self.logger.error(f"Gitee API request failed: {method} {url}, Error: {str(e)}")
            if hasattr(e, 'response') and e.response is not None:
                self.logger.error(f"Response status: {e.response.status_code}, body: {e.response.text}")
            return None

    def _parse_pr_url(self, pr_url: str) -> Tuple[str, str, int]:
        """
        Parse Gitee PR URL to extract owner, repo, and PR number
        
        Gitee PR URL format: https://gitee.com/{owner}/{repo}/pulls/{number}
        """
        parsed_url = urlparse(pr_url)
        path_parts = parsed_url.path.strip('/').split('/')
        
        if len(path_parts) < 4 or path_parts[2] not in ['pulls', 'pull']:
            raise ValueError(f"The provided URL does not appear to be a Gitee PR URL: {pr_url}")

        try:
            pr_number = int(path_parts[3])
        except ValueError as e:
            raise ValueError(f"Unable to convert PR number to integer: {path_parts[3]}") from e

        owner = path_parts[0]
        repo = path_parts[1]

        return owner, repo, pr_number

    def _set_repo_and_owner_from_pr(self):
        """Extract owner and repo from the PR URL"""
        try:
            owner, repo, pr_number = self._parse_pr_url(self.pr_url)
            self.owner = owner
            self.repo = repo
            self.pr_number = pr_number
            self.logger.info(f"Gitee PR - Owner: {self.owner}, Repo: {self.repo}, PR Number: {self.pr_number}")
        except ValueError as e:
            self.logger.error(f"Error parsing PR URL: {str(e)}")
            raise
        except Exception as e:
            self.logger.error(f"Unexpected error: {str(e)}")
            raise

    def _fetch_pr_data(self):
        """Fetch PR data and related information"""
        if not self.pr_number:
            return

        # Fetch PR details
        pr_data = self._api_request('GET', f"/repos/{self.owner}/{self.repo}/pulls/{self.pr_number}")
        if not pr_data:
            self.logger.error(f"Failed to fetch PR #{self.pr_number}")
            return
        
        # Wrap the dictionary in DictToObject for attribute-style access (like GitHub/GitLab)
        self.pr = DictToObject(pr_data)
        
        # Extract SHA and branch info
        if self.pr.head:
            self.sha = self.pr.head.sha
            self.base_sha = self.pr.base.sha if self.pr.base else ''
        
        # Fetch changed files
        self.git_files = self._api_request(
            'GET', 
            f"/repos/{self.owner}/{self.repo}/pulls/{self.pr_number}/files"
        ) or []
        
        # Filter ignored files
        if self.git_files:
            self.git_files = filter_ignored(self.git_files, platform="gitee")
        
        # Fetch commits
        self.pr_commits = self._api_request(
            'GET',
            f"/repos/{self.owner}/{self.repo}/pulls/{self.pr_number}/commits"
        ) or []
        
        if self.pr_commits:
            self.last_commit = self.pr_commits[-1]
        
        # Load file contents and diffs
        self._load_file_contents()
        self._load_file_diffs()

    def _load_file_contents(self):
        """
        Load file contents for all changed files from the PR's head commit.
        
        These contents represent the NEW/MODIFIED versions of files (head_file).
        They are cached in self.file_contents for later use in get_diff_files().
        
        NOTE: This only loads the PR source branch files (head_file), NOT the base branch files.
        Base branch files (base_file) are loaded separately via _get_file_content_from_base().
        """
        if not self.sha:
            return
            
        for file_info in self.git_files:
            filename = file_info.get('filename')
            if not filename or not is_valid_file(filename):
                continue
            
            try:
                # Fetch file content from PR head commit (source branch after changes)
                content_data = self._api_request(
                    'GET',
                    f"/repos/{self.owner}/{self.repo}/contents/{filename}",
                    params={'ref': self.sha}  # PR head commit SHA
                )
                
                if content_data and content_data.get('content'):
                    import base64
                    # Gitee API returns base64-encoded content, decode to get actual text
                    content = base64.b64decode(content_data['content']).decode('utf-8', errors='ignore')
                    self.file_contents[filename] = content  # This is head_file (NEW version)
                else:
                    self.file_contents[filename] = ""
                    
            except Exception as e:
                self.logger.error(f"Error getting file content for {filename}: {str(e)}")
                self.file_contents[filename] = ""

    def _load_file_diffs(self):
        """Load file diffs for all changed files from git_files metadata"""
        for file_info in self.git_files:
            filename = file_info.get('filename')
            if not filename:
                continue
            
            # Extract patch from file_info
            # Note: Gitee API returns patch as a dict object with diff field, or as a string
            patch_data = file_info.get('patch', '')
            # patch is a dict, extract the diff field
            patch = patch_data.get('diff', '')
            self.file_diffs[filename] = patch

    def is_supported(self, capability: str) -> bool:
        """Check if the capability is supported"""
        # Gitee does not support incremental review
        if capability in ['incremental_review']:
            return False
        # Gitee supports most other capabilities
        return True

    def get_incremental_commits(self, is_incremental):
        """
        Gitee does not support incremental review.
        Raises an exception if incremental review is requested.
        """
        if is_incremental and is_incremental.is_incremental:
            error_msg = "Incremental review (-i flag) is not supported for Gitee"
            self.logger.error(error_msg)
            raise NotImplementedError(error_msg)

    def get_pr_url(self) -> str:
        """Get PR URL"""
        return self.pr_url

    def get_issue_url(self) -> str:
        """
        Get Issue URL - Not supported for Gitee Provider.
        
        Raises:
            NotImplementedError: Always raised as Gitee Provider does not support Issues
        """
        error_msg = "get_issue_url() is not supported for Gitee Provider. Gitee Provider only supports Pull Requests."
        self.logger.error(error_msg)
        raise NotImplementedError(error_msg)

    def get_latest_commit_url(self) -> str:
        """Get latest commit URL"""
        if self.last_commit and self.last_commit.get('html_url'):
            return self.last_commit['html_url']
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
        
        API: POST /repos/{owner}/{repo}/pulls/{number}/comments
             POST /repos/{owner}/{repo}/issues/{number}/comments
        """
        if is_temporary and not get_settings().config.publish_output_progress:
            self.logger.debug(f"Skipping publish_comment for temporary comment")
            return None

        # Determine which endpoint to use - Gitee Provider only supports PRs
        if not self.enabled_pr:
            error_msg = "Cannot publish comment: Gitee Provider only supports Pull Request comments, not Issue comments."
            self.logger.error(error_msg)
            raise NotImplementedError(error_msg)
        
        index = self.pr_number
        endpoint = f"/repos/{self.owner}/{self.repo}/pulls/{index}/comments"

        comment = self.limit_output_characters(comment, self.max_comment_chars)
        
        # Gitee API expects 'body' field
        payload = {"body": comment}
        
        response = self._api_request('POST', endpoint, json=payload)
        
        if not response:
            self.logger.error("Failed to publish comment")
            return None

        if is_temporary:
            self.temp_comments.append({"comment": comment, "response": response})

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
        
        API: PATCH /repos/{owner}/{repo}/pulls/comments/{id}
             PATCH /repos/{owner}/{repo}/issues/comments/{id}
        """
        body = self.limit_output_characters(body, self.max_comment_chars)
        
        try:
            comment_id = comment.get("comment_id") if isinstance(comment, dict) else comment.get('id')
            if not comment_id:
                self.logger.error("Comment ID not found")
                return None
            
            # Determine endpoint based on comment type - Gitee Provider only supports PR comments
            if not self.enabled_pr:
                error_msg = "Cannot edit comment: Gitee Provider only supports Pull Request comments."
                self.logger.error(error_msg)
                raise NotImplementedError(error_msg)
            
            endpoint = f"/repos/{self.owner}/{self.repo}/pulls/comments/{comment_id}"
            
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
        # Gitee API: POST /repos/{owner}/{repo}/pulls/{number}/comments
        payload = {
            "body": body,
            "path": relevant_file.strip(),
            "position": absolute_position,  # Position in the diff
            "line": position  # Line number in the file
        }
        
        endpoint = f"/repos/{self.owner}/{self.repo}/pulls/{self.pr_number}/comments"
        response = self._api_request('POST', endpoint, json=payload)
        
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
            
            payload = {
                "body": body,
                "path": path,
            }
            
            if position is not None:
                payload["position"] = position
            if line is not None:
                payload["line"] = line
            
            endpoint = f"/repos/{self.owner}/{self.repo}/pulls/{self.pr_number}/comments"
            response = self._api_request('POST', endpoint, json=payload)
            
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
        
        self.logger.warning("Gitee does not support comment reactions")
        return None

    def remove_reaction(self, issue_comment_id: int, reaction_id: int) -> bool:
        """
        Remove reaction from a comment
        
        Note: Gitee doesn't natively support reactions
        """
        self.logger.warning("Gitee does not support comment reactions")
        return False

    def get_commit_messages(self) -> str:
        """
        Get commit messages for the PR
        
        API: GET /repos/{owner}/{repo}/pulls/{number}/commits
        """
        max_tokens = get_settings().get("CONFIG.MAX_COMMITS_TOKENS", None)
        
        if not self.pr_commits:
            self.logger.warning("No commits found")
            return ""

        try:
            commit_messages = []
            for commit in self.pr_commits:
                if commit and commit.get('commit'):
                    message = commit['commit'].get('message', '')
                    if message:
                        commit_messages.append(message)

            if not commit_messages:
                self.logger.warning("No commit messages found")
                return ""

            commit_message = "\n".join(commit_messages)
            
            if max_tokens:
                commit_message = clip_tokens(commit_message, max_tokens)

            return commit_message
            
        except Exception as e:
            self.logger.error(f"Error processing commit messages: {str(e)}")
            return ""

    def _get_file_content_from_base(self, filename: str) -> str:
        """
        Get file content from the base branch (target branch before PR merge).
        
        IMPORTANT: This is NOT a base64 encoding of head_file!
        - base_file: The ORIGINAL file content from the target branch (before changes)
        - head_file: The NEW file content from the PR source branch (after changes)
        
        Gitee API returns file content as base64-encoded string for safe transmission.
        We decode it to get the actual text content.
        
        Args:
            filename: Path to the file in the repository
            
        Returns:
            Decoded text content of the file from the base branch, or empty string if failed
        """
        if not self.base_sha:
            return ""
        
        try:
            # Fetch file content from base branch (target branch before PR)
            content_data = self._api_request(
                'GET',
                f"/repos/{self.owner}/{self.repo}/contents/{filename}",
                params={'ref': self.base_sha}  # Base branch commit SHA
            )
            
            if content_data and content_data.get('content'):
                import base64
                # Gitee API returns base64-encoded content, decode to get actual text
                return base64.b64decode(content_data['content']).decode('utf-8', errors='ignore')
            return ""
        except Exception as e:
            self.logger.error(f"Error getting base file content for {filename}: {e}")
            return ""

    def _get_file_content_from_latest_commit(self, filename: str) -> str:
        """
        Get file content from the latest commit in the PR (source branch).
        
        This returns the NEW/MODIFIED version of the file after PR changes.
        Content is pre-loaded in _load_file_contents() and cached in self.file_contents.
        
        Args:
            filename: Path to the file in the repository
            
        Returns:
            Text content of the file from the PR's latest commit, or empty string if not found
        """
        if not self.sha:
            return ""
        
        return self.file_contents.get(filename, "")

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
            link = f"{self.base_url}/{self.owner}/{self.repo}/blob/{branch}/{relevant_file}"
        elif relevant_line_end:
            link = f"{self.base_url}/{self.owner}/{self.repo}/blob/{branch}/{relevant_file}#L{relevant_line_start}-L{relevant_line_end}"
        else:
            link = f"{self.base_url}/{self.owner}/{self.repo}/blob/{branch}/{relevant_file}#L{relevant_line_start}"

        return link

    def get_pr_id(self) -> str:
        """Get PR identifier"""
        try:
            return f"{self.owner}/{self.repo}#{self.pr_number}"
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
        Get all comments on the PR - Issues are not supported by Gitee Provider.
        
        API: GET /repos/{owner}/{repo}/pulls/{number}/comments
        
        Raises:
            NotImplementedError: If called in non-PR context
        """
        if not self.enabled_pr:
            error_msg = "get_issue_comments() is not supported for Issues in Gitee Provider. Only Pull Request comments are supported."
            self.logger.error(error_msg)
            raise NotImplementedError(error_msg)
        
        index = self.pr_number
        endpoint = f"/repos/{self.owner}/{self.repo}/pulls/{index}/comments"

        comments = self._api_request('GET', endpoint)
        
        if not comments:
            self.logger.warning("Failed to get comments or no comments found")
            return []

        # Convert dict list to DictToObject list for consistent API usage
        comments_list = comments if isinstance(comments, list) else []
        return [DictToObject(comment) for comment in comments_list]

    def get_languages(self):
        """
        Get programming languages used in the repository
        
        API: GET /repos/{owner}/{repo}/languages
        Response format: {"languages": [{"language": "Shell", "color": "#89e051", "percent": 98.3, "bytes": 1432676}, ...]}
        
        Returns:
            dict: A dictionary where each key is a language name and the value is the percentage
        """
        try:
            languages_data = self._api_request('GET', f"/repos/{self.owner}/{self.repo}/languages")
            
            if not languages_data or not isinstance(languages_data, dict):
                return {}
            
            # Gitee API returns {"languages": [{"language": "...", "percent": ..., ...}, ...]}
            languages_list = languages_data.get('languages', [])
            
            if not isinstance(languages_list, list):
                return {}
            
            # Extract language names and percentages from the list
            # Return format matches GitHub/GitLab: {language: percentage}
            return {lang_info['language']: lang_info['percent'] 
                    for lang_info in languages_list 
                    if lang_info.get('language') and 'percent' in lang_info}
            
        except Exception as e:
            self.logger.error(f"Error getting languages: {e}")
            return {}

    def get_pr_branch(self) -> str:
        """Get the source branch name of the PR"""
        if not self.pr:
            self.logger.error("PR data not loaded")
            return ""

        head = self.pr.head if hasattr(self.pr, 'head') else None
        if not head:
            self.logger.error("PR head not found")
            return ""

        return head.ref if hasattr(head, 'ref') else ''

    def get_pr_description_full(self) -> str:
        """Get full PR description"""
        if not self.pr:
            self.logger.error("PR data not loaded")
            return ""

        return self.pr.body or ""

    def get_pr_labels(self, update: bool = False) -> List[str]:
        """
        Get labels assigned to the PR
        
        API: GET /repos/{owner}/{repo}/pulls/{number}/labels
        """
        if not update and self.pr:
            labels = self.pr.labels if hasattr(self.pr, 'labels') else []
            return [label.name for label in labels if hasattr(label, 'name') and label.name]

        # Fetch fresh labels
        labels_data = self._api_request(
            'GET',
            f"/repos/{self.owner}/{self.repo}/pulls/{self.pr_number}/labels"
        )
        
        if not labels_data or not isinstance(labels_data, list):
            self.logger.warning("PR has no labels")
            return []

        return [label.get('name', '') for label in labels_data if label.get('name')]

    def get_repo_settings(self) -> str:
        """
        Get repository settings file content
        
        Reads the file specified in GITEE.REPO_SETTING config
        """
        if not self.repo_settings:
            self.logger.debug("Repository settings file not configured")
            return ""

        try:
            content_data = self._api_request(
                'GET',
                f"/repos/{self.owner}/{self.repo}/contents/{self.repo_settings}",
                params={'ref': self.sha or self.get_pr_branch()}
            )
            
            if not content_data or not content_data.get('content'):
                self.logger.warning(f"Settings file '{self.repo_settings}' not found or empty")
                return ""

            import base64
            return base64.b64decode(content_data['content']).decode('utf-8', errors='ignore')
            
        except Exception as e:
            self.logger.error(f"Error reading repo settings: {e}")
            return ""

    def get_user_id(self) -> str:
        """Get the authenticated user's ID"""
        if self.pr and hasattr(self.pr, 'user'):
            return str(self.pr.user.id) if hasattr(self.pr.user, 'id') else ""
        return ""

    def get_git_repo_url(self, issues_or_pr_url: str) -> str:
        """Get Git clone URL for the repository"""
        return f"{self.base_url}/{self.owner}/{self.repo}.git"

    def publish_description(self, pr_title: str, pr_body: str) -> Optional[bool]:
        """
        Update PR title and description - Issues are not supported.
        
        API: PATCH /repos/{owner}/{repo}/pulls/{number}
        
        Raises:
            NotImplementedError: If called in non-PR context
        """
        if not self.enabled_pr:
            error_msg = "publish_description() is not supported for Issues in Gitee Provider. Only Pull Requests are supported."
            self.logger.error(error_msg)
            raise NotImplementedError(error_msg)
        
        index = self.pr_number
        
        payload = {
            "title": pr_title,
            "body": pr_body
        }
        
        endpoint = f"/repos/{self.owner}/{self.repo}/pulls/{index}"
        response = self._api_request('PATCH', endpoint, json=payload)
        
        if not response:
            self.logger.error("Failed to update PR description")
            return False

        # Refresh PR data
        self.pr = DictToObject(self._api_request('GET', endpoint))
        
        self.logger.info("PR description updated successfully")
        return True

    def publish_labels(self, labels: List[str]) -> Optional[bool]:
        """
        Add labels to the PR
        
        API: POST /repos/{owner}/{repo}/pulls/{number}/labels
        Note: Gitee expects labels as a JSON array string in request body
        Example: '["feat", "bug"]'
        
        Gitee label name restrictions:
        - Only allows: Chinese characters, letters, digits, dots(.), underscores(_), hyphens(-), slashes(/), backslashes(\\)
        - Length must be between 2 and 20 characters
        - Spaces are NOT allowed
        """
        if not labels:
            self.logger.warning("No labels provided")
            return False

        # Must use PR endpoint, not issues endpoint
        if not self.enabled_pr:
            self.logger.warning("Cannot add labels: not a PR context")
            return False
        
        # Sanitize labels for Gitee compatibility
        sanitized_labels = []
        for label in labels:
            # Replace spaces with underscores (Gitee doesn't allow spaces)
            sanitized = label.replace(' ', '_')
            # Ensure length is within 2-20 characters
            if len(sanitized) < 2:
                sanitized = sanitized + '_pad'
            elif len(sanitized) > 20:
                sanitized = sanitized[:20]
            sanitized_labels.append(sanitized)
        
        # Gitee expects labels as a JSON array string
        labels_json = json.dumps(sanitized_labels)
        
        endpoint = f"/repos/{self.owner}/{self.repo}/pulls/{self.pr_number}/labels"
        response = self._api_request('POST', endpoint, data=labels_json)
        
        if response:
            self.logger.info(f"Labels added successfully: {sanitized_labels}")
            return True
        else:
            self.logger.error("Failed to add labels")
            return False

    def remove_comment(self, comment) -> None:
        """
        Remove a specific comment
        
        API: DELETE /repos/{owner}/{repo}/pulls/comments/{id}
             DELETE /repos/{owner}/{repo}/issues/comments/{id}
        """
        if not comment:
            return

        try:
            comment_id = comment.get("comment_id") if isinstance(comment, dict) else comment.get('id')
            if not comment_id:
                self.logger.error("Comment ID not found")
                return
            
            # Determine endpoint - Gitee Provider only supports PR comments
            if not self.enabled_pr:
                error_msg = "Cannot remove comment: Gitee Provider only supports Pull Request comments."
                self.logger.error(error_msg)
                raise NotImplementedError(error_msg)
            
            endpoint = f"/repos/{self.owner}/{self.repo}/pulls/comments/{comment_id}"
            
            result = self._api_request('DELETE', endpoint)
            
            if result:
                # Remove from local list
                self.comments_list = [c for c in self.comments_list if c.get('comment_id') != comment_id]
                self.logger.info(f"Comment {comment_id} removed successfully")
            else:
                self.logger.error(f"Failed to remove comment {comment_id}")
                
        except Exception as e:
            self.logger.error(f"Error removing comment: {e}")
            raise

    def remove_initial_comment(self) -> None:
        """Remove all temporary comments"""
        for comment in self.temp_comments:
            try:
                if isinstance(comment, dict) and comment.get('comment_id'):
                    self.remove_comment(comment)
            except Exception as e:
                self.logger.error(f"Error removing temporary comment: {e}")
        
        self.temp_comments.clear()

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
