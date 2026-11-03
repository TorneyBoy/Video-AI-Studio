"""Explicit OSS credential sources, including renewable ECS instance roles."""

from datetime import datetime, timezone
import re


def oss_credentials(mode: str, *, role_name: str | None = None, role_provider=None):
    import alibabacloud_oss_v2 as oss

    if mode == "environment":
        return oss.credentials.EnvironmentVariableCredentialsProvider()
    if mode != "ecs_ram_role" or not role_name or not re.fullmatch(r"[A-Za-z0-9.@_-]{1,64}", role_name):
        raise ValueError("An explicit ECS role name or environment credential mode is required")
    if role_provider is None:
        from alibabacloud_credentials.provider import EcsRamRoleCredentialsProvider

        role_provider = EcsRamRoleCredentialsProvider(role_name=role_name, disable_imds_v1=True,
                                                      async_update_enabled=False)

    def fresh_credentials():
        value = role_provider.get_credentials()
        expiration = value.get_expiration()
        if not expiration or expiration <= datetime.now(timezone.utc).timestamp():
            raise ValueError("ECS instance-role credentials are unavailable or expired")
        return oss.Credentials(value.get_access_key_id(), value.get_access_key_secret(), value.get_security_token(),
                               expiration=datetime.fromtimestamp(expiration, timezone.utc))

    return oss.credentials.CredentialsProviderFunc(fresh_credentials)
