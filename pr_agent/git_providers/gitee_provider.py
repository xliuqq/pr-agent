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
                                                 FilePatchInfo, GitProvider,
                                                 IncrementalPR)
from pr_agent.log import get_logger


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
        self.issue_url = ""

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
        self.issue_number = None
        self.max_comment_chars = 65000
        self.enabled_pr = False
        self.enabled_issue = False
        self.temp_comments = []
        self.pr = None
        self.git_files = []
        self.file_contents = {}
        self.file_diffs = {}
        self.sha = None
        self.diff_files = []
        self.incremental = IncrementalPR(False)
        self.comments_list = []
        self.unreviewed_files_set = dict()
        self.pr_commits = None
        self.last_commit = None
        self.base_sha = None
        self.base_ref = None

        # Parse URL and initialize
        if "pulls" in url or "pull" in url:
            self.pr_url = url
            self._set_repo_and_owner_from_pr()
            self.enabled_pr = True
            self._fetch_pr_data()
        elif "issues" in url:
            self.issue_url = url
            self._set_repo_and_owner_from_issue()
            self.enabled_issue = True
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

    def _parse_issue_url(self, issue_url: str) -> Tuple[str, str, int]:
        """
        Parse Gitee Issue URL to extract owner, repo, and issue number
        
        Gitee Issue URL format: https://gitee.com/{owner}/{repo}/issues/{number}
        """
        parsed_url = urlparse(issue_url)
        path_parts = parsed_url.path.strip('/').split('/')
        
        if len(path_parts) < 4 or path_parts[2] != 'issues':
            raise ValueError(f"The provided URL does not appear to be a Gitee issue URL: {issue_url}")

        try:
            issue_number = int(path_parts[3])
        except ValueError as e:
            raise ValueError(f"Unable to convert issue number to integer: {path_parts[3]}") from e

        owner = path_parts[0]
        repo = path_parts[1]

        return owner, repo, issue_number

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

    def _set_repo_and_owner_from_issue(self):
        """Extract owner and repo from the issue URL"""
        try:
            owner, repo, issue_number = self._parse_issue_url(self.issue_url)
            self.owner = owner
            self.repo = repo
            self.issue_number = issue_number
            self.logger.info(f"Gitee Issue - Owner: {self.owner}, Repo: {self.repo}, Issue Number: {self.issue_number}")
        except ValueError as e:
            self.logger.error(f"Error parsing issue URL: {str(e)}")
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
        
        self.pr = pr_data
        
        # Extract SHA and branch info
        if self.pr.get('head'):
            self.sha = self.pr['head'].get('sha', '')
            self.base_sha = self.pr.get('base', {}).get('sha', '')
            self.base_ref = self.pr.get('base', {}).get('ref', '')
        
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
        """Load file contents for all changed files"""
        if not self.sha:
            return
            
        for file_info in self.git_files:
            filename = file_info.get('filename')
            if not filename or not is_valid_file(filename):
                continue
            
            try:
                # Get file content from the PR head commit
                content_data = self._api_request(
                    'GET',
                    f"/repos/{self.owner}/{self.repo}/contents/{filename}",
                    params={'ref': self.sha}
                )
                
                if content_data and content_data.get('content'):
                    import base64
                    content = base64.b64decode(content_data['content']).decode('utf-8', errors='ignore')
                    self.file_contents[filename] = content
                else:
                    self.file_contents[filename] = ""
                    
            except Exception as e:
                self.logger.error(f"Error getting file content for {filename}: {str(e)}")
                self.file_contents[filename] = ""

    def _load_file_diffs(self):
        """Load file diffs for all changed files"""
        try:
            # Get diff/patch for the PR
            diff_response = self._api_request(
                'GET',
                f"/repos/{self.owner}/{self.repo}/pulls/{self.pr_number}",
                params={'accept': 'application/vnd.github.v3.diff'}
            )
            
            if not diff_response or not isinstance(diff_response, str):
                # Fallback: construct diffs from file info
                for file_info in self.git_files:
                    filename = file_info.get('filename')
                    if filename:
                        self.file_diffs[filename] = file_info.get('patch', '')
                return
            
            # Parse unified diff
            lines = diff_response.splitlines()
            current_file = None
            current_patch = []
            
            for line in lines:
                if line.startswith('diff --git'):
                    if current_file and current_patch:
                        self.file_diffs[current_file] = '\n'.join(current_patch)
                        current_patch = []
                    # Extract filename from "diff --git a/path b/path"
                    parts = line.split(' b/')
                    if len(parts) > 1:
                        current_file = parts[1]
                elif line.startswith('@@'):
                    current_patch = [line]
                elif current_patch:
                    current_patch.append(line)
            
            if current_file and current_patch:
                self.file_diffs[current_file] = '\n'.join(current_patch)
                
        except Exception as e:
            self.logger.error(f"Error loading file diffs: {str(e)}")
            # Fallback to patch from file info
            for file_info in self.git_files:
                filename = file_info.get('filename')
                if filename:
                    self.file_diffs[filename] = file_info.get('patch', '')

    def is_supported(self, capability: str) -> bool:
        """Check if the capability is supported"""
        # Gitee supports most capabilities
        return True

    def get_pr_url(self) -> str:
        """Get PR URL"""
        return self.pr_url

    def get_issue_url(self) -> str:
        """Get Issue URL"""
        return self.issue_url

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

        # Determine which endpoint to use
        if self.enabled_issue:
            index = self.issue_number
            endpoint = f"/repos/{self.owner}/{self.repo}/issues/{index}/comments"
        elif self.enabled_pr:
            index = self.pr_number
            endpoint = f"/repos/{self.owner}/{self.repo}/pulls/{index}/comments"
        else:
            self.logger.error("Neither PR nor issue URL provided.")
            return None

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
            
            # Determine endpoint based on comment type
            if self.enabled_pr:
                endpoint = f"/repos/{self.owner}/{self.repo}/pulls/comments/{comment_id}"
            else:
                endpoint = f"/repos/{self.owner}/{self.repo}/issues/comments/{comment_id}"
            
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
        """Get file content from base branch"""
        if not self.base_sha:
            return ""
        
        try:
            content_data = self._api_request(
                'GET',
                f"/repos/{self.owner}/{self.repo}/contents/{filename}",
                params={'ref': self.base_sha}
            )
            
            if content_data and content_data.get('content'):
                import base64
                return base64.b64decode(content_data['content']).decode('utf-8', errors='ignore')
            return ""
        except Exception as e:
            self.logger.error(f"Error getting base file content for {filename}: {e}")
            return ""

    def _get_file_content_from_latest_commit(self, filename: str) -> str:
        """Get file content from latest commit"""
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

            # Limit full file loading for large PRs
            if counter_valid >= MAX_FILES_ALLOWED_FULL and patch and not self.incremental.is_incremental:
                avoid_load = True
                if counter_valid == MAX_FILES_ALLOWED_FULL:
                    self.logger.info("Too many files in PR, will avoid loading full content for rest of files")

            # Get head file content
            if avoid_load:
                head_file = ""
            else:
                head_file = self.file_contents.get(filename, "")

            # Get base file content
            if self.incremental.is_incremental and self.unreviewed_files_set:
                base_file = self._get_file_content_from_latest_commit(filename)
                self.unreviewed_files_set[filename] = patch
            else:
                if avoid_load:
                    base_file = ""
                else:
                    base_file = self._get_file_content_from_base(filename)

            # Get line counts
            num_plus_lines = file_info.get('additions', 0)
            num_minus_lines = file_info.get('deletions', 0)
            status = file_info.get('status', '')

            # Determine edit type
            if status == 'added':
                edit_type = EDIT_TYPE.ADDED
            elif status in ['removed', 'deleted']:
                edit_type = EDIT_TYPE.DELETED
            elif status == 'renamed':
                edit_type = EDIT_TYPE.RENAMED
            elif status in ['modified', 'changed']:
                edit_type = EDIT_TYPE.MODIFIED
            else:
                self.logger.error(f"Unknown edit type: {status}")
                edit_type = EDIT_TYPE.UNKNOWN

            file_patch_info = FilePatchInfo(
                base_file=base_file,
                head_file=head_file,
                patch=patch,
                filename=filename,
                num_minus_lines=num_minus_lines,
                num_plus_lines=num_plus_lines,
                edit_type=edit_type
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
        Get all comments on the PR or Issue
        
        API: GET /repos/{owner}/{repo}/pulls/{number}/comments
             GET /repos/{owner}/{repo}/issues/{number}/comments
        """
        if self.enabled_issue:
            index = self.issue_number
            endpoint = f"/repos/{self.owner}/{self.repo}/issues/{index}/comments"
        elif self.enabled_pr:
            index = self.pr_number
            endpoint = f"/repos/{self.owner}/{self.repo}/pulls/{index}/comments"
        else:
            self.logger.error("Neither PR nor issue URL provided.")
            return []

        comments = self._api_request('GET', endpoint)
        
        if not comments:
            self.logger.warning("Failed to get comments or no comments found")
            return []

        return comments if isinstance(comments, list) else []

    def get_languages(self) -> Set[str]:
        """
        Get programming languages used in the repository
        
        API: GET /repos/{owner}/{repo}/languages
        """
        try:
            languages_data = self._api_request('GET', f"/repos/{self.owner}/{self.repo}/languages")
            
            if not languages_data or not isinstance(languages_data, dict):
                return set()
            
            # Return language names
            return set(languages_data.keys())
            
        except Exception as e:
            self.logger.error(f"Error getting languages: {e}")
            return set()

    def get_pr_branch(self) -> str:
        """Get the source branch name of the PR"""
        if not self.pr:
            self.logger.error("PR data not loaded")
            return ""

        head = self.pr.get('head')
        if not head:
            self.logger.error("PR head not found")
            return ""

        return head.get('ref', '')

    def get_pr_description_full(self) -> str:
        """Get full PR description"""
        if not self.pr:
            self.logger.error("PR data not loaded")
            return ""

        return self.pr.get('body', '') or ""

    def get_pr_labels(self, update: bool = False) -> List[str]:
        """
        Get labels assigned to the PR
        
        API: GET /repos/{owner}/{repo}/pulls/{number}/labels
        """
        if not update and self.pr:
            labels = self.pr.get('labels', [])
            return [label.get('name', '') for label in labels if label.get('name')]

        # Fetch fresh labels
        labels_data = self._api_request(
            'GET',
            f"/repos/{self.owner}/{self.repo}/pulls/{self.pr_number}/labels"
        )
        
        if not labels_data or not isinstance(labels_data, list):
            self.logger.warning("Failed to get PR labels")
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
        if self.pr and self.pr.get('user'):
            return str(self.pr['user'].get('id', ''))
        return ""

    def get_git_repo_url(self, issues_or_pr_url: str) -> str:
        """Get Git clone URL for the repository"""
        return f"{self.base_url}/{self.owner}/{self.repo}.git"

    def publish_description(self, pr_title: str, pr_body: str) -> Optional[bool]:
        """
        Update PR title and description
        
        API: PATCH /repos/{owner}/{repo}/pulls/{number}
        """
        index = self.pr_number if self.enabled_pr else self.issue_number
        
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
        if self.enabled_pr:
            self.pr = self._api_request('GET', endpoint)
        
        self.logger.info("PR description updated successfully")
        return True

    def publish_labels(self, labels: List[str]) -> Optional[bool]:
        """
        Add labels to the PR
        
        API: POST /repos/{owner}/{repo}/issues/{number}/labels
        Note: In Gitee, PRs are treated as Issues for labeling
        """
        if not labels:
            self.logger.warning("No labels provided")
            return False

        index = self.pr_number if self.enabled_pr else self.issue_number
        
        # Gitee expects label names in the request
        payload = {"labels": labels}
        
        endpoint = f"/repos/{self.owner}/{self.repo}/issues/{index}/labels"
        response = self._api_request('POST', endpoint, json=payload)
        
        if response:
            self.logger.info(f"Labels added successfully: {labels}")
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
            
            # Determine endpoint
            if self.enabled_pr:
                endpoint = f"/repos/{self.owner}/{self.repo}/pulls/comments/{comment_id}"
            else:
                endpoint = f"/repos/{self.owner}/{self.repo}/issues/comments/{comment_id}"
            
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
