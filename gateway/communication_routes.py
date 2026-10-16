"""Opt-in, machine-ID Discord delivery routing; never changes stored origins or topics.

Reader evidence comes from fresh REST channel overwrites, not the guild member cache.
The policy intentionally refuses private threads and unknown/incomplete permission evidence.
"""
from dataclasses import dataclass
from typing import Any, Mapping


class CommunicationPolicyError(ValueError):
    """No authorized delivery surface could be proved."""


@dataclass(frozen=True)
class CommunicationPolicy:
    enabled: bool
    primary_channels: Mapping[str, str]
    routes: Mapping[str, str]
    owner_ids: frozenset[str]
    private_output_channels: frozenset[str] = frozenset()
    existing_admin_bot_ids: frozenset[str] = frozenset()

    @classmethod
    def from_config(cls, config):
        config = config if isinstance(config, dict) else {}
        enabled = config.get('enabled') is True
        primary = config.get('primary_channels', {})
        routes = config.get('routes', {})
        if not isinstance(primary, dict) or not isinstance(routes, dict):
            raise CommunicationPolicyError('communication configuration must contain ID mappings')
        primary = {str(k): str(v) for k, v in primary.items()}
        routes = {str(k): str(v) for k, v in routes.items()}
        owners = frozenset(str(v) for v in config.get('owner_ids', []))
        private = frozenset(str(v) for v in config.get('private_output_channels', []))
        admin_bots = frozenset(str(v) for v in config.get('existing_admin_bot_ids', []))
        ids = [*primary.values(), *routes.keys(), *routes.values(), *owners, *private, *admin_bots]
        if enabled and (set(primary) != {'personal', 'work', 'development'}
                        or len(set(primary.values())) != 3
                        or any(not value.isdecimal() for value in ids)):
            raise CommunicationPolicyError('communication requires three distinct numeric primary channel IDs')
        return cls(enabled, primary, routes, owners, private, admin_bots)

    def as_config(self):
        return dict(enabled=self.enabled, primary_channels=dict(self.primary_channels),
                    routes=dict(self.routes), owner_ids=sorted(self.owner_ids),
                    private_output_channels=sorted(self.private_output_channels),
                    existing_admin_bot_ids=sorted(self.existing_admin_bot_ids))

    def accepts_input(self, channel_id, parent_id=None):
        return (not self.enabled or str(channel_id) in self.primary_channels.values()
                or (parent_id is not None and str(parent_id) in self.primary_channels.values()))

    def redirect(self, channel_id, parent_id=None):
        return self.routes.get(str(channel_id)) or self.routes.get(str(parent_id))


@dataclass(frozen=True)
class DeliveryTarget:
    channel: Any
    source_id: str
    target_id: str
    moved: bool


def _kind(channel):
    value = getattr(channel, 'type', None)
    return getattr(value, 'value', value)


def _acl(channel):
    # discord.py's public overwrites property resolves against cached roles and can OMIT
    # unknown role targets. Raw REST-decoded overwrites retain every role/member ID.
    raw = getattr(channel, '_overwrites', None)
    if not isinstance(raw, (list, tuple)):
        raise CommunicationPolicyError('reader ACL evidence unavailable')
    return tuple(sorted((int(o.id), int(o.type), int(o.allow), int(o.deny)) for o in raw))


async def _surface(client, channel):
    parent_id = getattr(channel, 'parent_id', None)
    if parent_id:
        if _kind(channel) not in (10, 11):
            raise CommunicationPolicyError('private/unknown thread reader scope is unproved')
        return await client.fetch_channel(int(parent_id))
    return channel


async def _restricted_reader_scope(client, channel, policy):
    """Prove owner/sender readers plus explicitly recorded, current guild admin bots.

    Existing admin bot IDs grant no permissions: every call requires fresh REST roles,
    managed=True, the matching bot tag and administrator=True. Such bots already bypass
    every channel ACL in this guild. Ordinary roles/member grants never inherit this
    exception from a raw configuration entry or a cached member list.
    """
    guild_id = getattr(getattr(channel, 'guild', None), 'id', None)
    bot_id = str(getattr(getattr(client, 'user', None), 'id', ''))
    if not guild_id or not bot_id:
        return False
    guild = await client.fetch_guild(int(guild_id))
    if str(guild.owner_id) not in policy.owner_ids:
        return False
    roles = await guild.fetch_roles()
    entries = _acl(channel)
    overwrite_by_role = {oid: (allow, deny) for oid, kind, allow, deny in entries if kind == 0}
    view = 1 << 10
    default_allow, default_deny = overwrite_by_role.get(int(guild_id), (0, 0))
    default_role = next((r for r in roles if int(r.id) == int(guild_id)), None)
    if default_role is None:
        return False
    everyone_view = bool(getattr(default_role.permissions, 'view_channel', False))
    everyone_view = (everyone_view and not default_deny & view) or bool(default_allow & view)
    if everyone_view:
        return False
    role_ids = {int(role.id) for role in roles}
    if any(kind == 0 and oid not in role_ids for oid, kind, _, _ in entries):
        return False

    def managed_bot_id(role):
        if getattr(role, 'managed', False) is not True:
            return None
        value = getattr(getattr(role, 'tags', None), 'bot_id', None)
        return str(value) if value is not None else None

    verified_admin_bots = {
        managed_bot_id(role) for role in roles
        if managed_bot_id(role) in policy.existing_admin_bot_ids
        and getattr(role.permissions, 'administrator', False) is True
    }
    if policy.existing_admin_bot_ids - verified_admin_bots:
        return False  # Revoked/missing/wrong-tag evidence does not become a member allowlist.
    allowed_bot_ids = verified_admin_bots | {bot_id}
    everyone_base_view = bool(getattr(default_role.permissions, 'view_channel', False))
    for role in roles:
        trusted_bot_role = managed_bot_id(role) in allowed_bot_ids
        if getattr(role.permissions, 'administrator', False) and not trusted_bot_role:
            return False
        # Discord ORs guild role base permissions, then applies @everyone overwrite,
        # aggregate role denies and aggregate role allows. Testing every non-bot role
        # plus rejecting every effective role allow proves no combination adds VIEW.
        base_view = everyone_base_view or bool(getattr(role.permissions, 'view_channel', False))
        after_everyone = (base_view and not default_deny & view) or bool(default_allow & view)
        allow, deny = overwrite_by_role.get(int(role.id), (0, 0))
        effective_view = (after_everyone and not deny & view) or bool(allow & view)
        if effective_view and not trusted_bot_role:
            return False
    allowed_members = policy.owner_ids | allowed_bot_ids
    return not any(kind == 1 and allow & view and str(oid) not in allowed_members
                   for oid, kind, allow, _ in entries)



async def resolve_discord_delivery(client, policy, chat_id, metadata=None, *, editing=False):
    """Resolve only the delivery reference and prove its audience before any payload is sent.

    ``communication_source_channel_id`` is an existing trusted source boundary, not a
    user/role allowlist. Direct destinations require verified restricted readers; optional
    existing admin bots must have current managed administrator roles in this guild.
    A relocated edit is rejected: message IDs cannot be transplanted between channels.
    """
    metadata = metadata or {}
    requested = str(metadata.get('thread_id') or chat_id)
    if not policy.enabled:
        channel = client.get_channel(int(requested))
        if not channel:
            channel = await client.fetch_channel(int(requested))
        return DeliveryTarget(channel, requested, requested, False)
    source = await client.fetch_channel(int(requested))
    if source is None:
        raise CommunicationPolicyError('source channel unavailable')
    source_parent = getattr(source, 'parent_id', None)
    explicit = policy.routes.get(requested)
    if source_parent and not policy.accepts_input(requested, source_parent) and not explicit:
        raise CommunicationPolicyError('legacy thread requires an explicit thread route')
    target_id = explicit or requested
    moved = target_id != requested
    if editing and moved:
        raise CommunicationPolicyError('cannot edit a message in a relocated channel')
    target = await client.fetch_channel(int(target_id)) if moved else source
    if target is None or (not policy.accepts_input(target_id, getattr(target, 'parent_id', None))
                          and target_id not in policy.private_output_channels):
        raise CommunicationPolicyError('unmapped communication destination')
    # A metadata thread must belong to the declared chat, unless chat itself is that thread.
    if metadata.get('thread_id') and str(chat_id) != requested and str(source_parent) != str(chat_id):
        raise CommunicationPolicyError('thread does not belong to requested parent')
    target_surface = await _surface(client, target)
    source_id = str(metadata.get('communication_source_channel_id') or requested)
    # Route source is an existing boundary. A direct primary destination alone does not
    # prove which confidential sources a model consulted, so require restricted-reader evidence.
    has_source_boundary = moved or bool(metadata.get('communication_source_channel_id'))
    if has_source_boundary:
        boundary = await client.fetch_channel(int(source_id))
        boundary_surface = await _surface(client, boundary)
        source_guild = getattr(getattr(boundary_surface, 'guild', None), 'id', None)
        target_guild = getattr(getattr(target_surface, 'guild', None), 'id', None)
        if not source_guild or source_guild != target_guild:
            raise CommunicationPolicyError('cross-guild source reader boundary is unproved')
        legacy_boundary = not policy.accepts_input(
            source_id, getattr(boundary, 'parent_id', None),
        )
        if (legacy_boundary and source_guild and source_guild == target_guild
                and _acl(boundary_surface) == _acl(target_surface)):
            return DeliveryTarget(target, source_id, target_id, moved)
    if not await _restricted_reader_scope(client, target_surface, policy):
        raise CommunicationPolicyError('reader scope is broader than source or unproved')
    return DeliveryTarget(target, source_id, target_id, moved)


async def verify_discord_voice_audience(client, policy, voice_channel_id, source_id):
    """Prove potential VC listeners from fresh channel ACLs, never current member cache.

    A VC is not an extra input/output text destination. This only authorizes playback;
    failed/unknown evidence must fall back to guarded text-channel attachments.
    """
    if not policy.enabled:
        return True
    if not source_id:
        return False
    voice = await client.fetch_channel(int(voice_channel_id))
    if _kind(voice) not in (2, 13):
        return False
    source = await client.fetch_channel(int(source_id))
    boundary = await _surface(client, source)
    source_guild = getattr(getattr(boundary, 'guild', None), 'id', None)
    voice_guild = getattr(getattr(voice, 'guild', None), 'id', None)
    if not source_guild or source_guild != voice_guild:
        return False
    if (not policy.accepts_input(source_id, getattr(source, 'parent_id', None))
            and _acl(boundary) == _acl(voice)):
        return True
    return await _restricted_reader_scope(client, voice, policy)
