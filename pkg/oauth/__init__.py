from .github_oauth import GithubOAuth
from .gitee_oauth import GiteeOAuth
from .oauth import OAuthUserInfo, OAuth

__all__ = ["OAuthUserInfo", "OAuth", "GithubOAuth", "GiteeOAuth"]
