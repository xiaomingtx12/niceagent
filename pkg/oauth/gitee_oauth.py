import urllib.parse

import requests

from .oauth import OAuth, OAuthUserInfo


class GiteeOAuth(OAuth):
    """Gitee OAuth第三方授权认证类"""

    _AUTHORIZE_URL = "https://gitee.com/oauth/authorize"  # 跳转授权接口
    _ACCESS_TOKEN_URL = "https://gitee.com/oauth/token"  # 获取授权令牌接口
    _USER_INFO_URL = "https://gitee.com/api/v5/user"  # 获取用户信息接口

    def get_provider(self) -> str:
        return "gitee"

    def get_authorization_url(self) -> str:
        params = {
            "client_id": self.client_id,
            "redirect_uri": self.redirect_uri,
            "response_type": "code",
            # 说明：Gitee是否返回 email 依赖用户授权与 scope，这里尽量申请 email 权限；
            # 若最终拿不到 email，后续会在 _transform_user_info 中兜底生成 no-reply 邮箱。
            "scope": "user_info emails",
        }
        return f"{self._AUTHORIZE_URL}?{urllib.parse.urlencode(params)}"

    def get_access_token(self, code: str) -> str:
        # 1.组装请求数据
        data = {
            "grant_type": "authorization_code",
            "code": code,
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "redirect_uri": self.redirect_uri,
        }
        headers = {"Accept": "application/json"}

        # 2.发起post请求并获取相应的数据
        resp = requests.post(self._ACCESS_TOKEN_URL, data=data, headers=headers)
        resp.raise_for_status()
        resp_json = resp.json()

        # 3.提取access_token对应的数据
        access_token = resp_json.get("access_token")
        if not access_token:
            raise ValueError(f"Gitee OAuth授权失败: {resp_json}")

        return access_token

    def get_raw_user_info(self, token: str) -> dict:
        # Gitee API v5 常用方式：通过 querystring 传递 access_token
        resp = requests.get(self._USER_INFO_URL, params={"access_token": token})
        resp.raise_for_status()
        return resp.json()

    def _transform_user_info(self, raw_info: dict) -> OAuthUserInfo:
        # 1.提取邮箱，如果不存在设置一个默认邮箱
        email = raw_info.get("email")
        if not email:
            login = raw_info.get("login") or "user"
            email = f"{raw_info.get('id')}+{login}@user.no-reply@gitee.com"

        # 2.提取姓名，优先 name，不存在则用 login
        name = raw_info.get("name") or raw_info.get("login") or str(raw_info.get("id"))

        return OAuthUserInfo(
            id=str(raw_info.get("id")),
            name=str(name),
            email=str(email),
        )

