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
    shared_participant_bot_ids: frozenset[str] = frozenset()

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
        shared_bots = frozenset(str(v) for v in config.get('shared_participant_bot_ids', []))
        ids = [*primary.values(), *routes.keys(), *routes.values(), *owners, *private, *admin_bots, *shared_bots]
        if enabled and (set(primary) != {'personal', 'work', 'development'}
                        or len(set(primary.values())) != 3
                        or any(not value.isdecimal() for value in ids)):
            raise CommunicationPolicyError('communication requires three distinct numeric primary channel IDs')
        return cls(enabled, primary, routes, owners, private, admin_bots, shared_bots)

    def as_config(self):
        return dict(enabled=self.enabled, primary_channels=dict(self.primary_channels),
                    routes=dict(self.routes), owner_ids=sorted(self.owner_ids),
                    private_output_channels=sorted(self.private_output_channels),
                    existing_admin_bot_ids=sorted(self.existing_admin_bot_ids),
                    shared_participant_bot_ids=sorted(self.shared_participant_bot_ids))

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


async def _fresh_member_roles(client, guild_id, role_ids, owner_id):
    """Read one complete raw REST page; a full page reaches the 1000-member HOLD cap.

    discord.py Guild.fetch_members uses HTTPClient.get_members(id, 1000, after)
    and treats a short page as terminal. Raw IDs avoid Member.roles' guild cache.
    No cached members, approximate counts, or truncated pagination prove readers.
    """
    try:
        data = await client.http.get_members(int(guild_id), 1000, None)
    except Exception as exc:
        raise CommunicationPolicyError('fresh complete member evidence unavailable') from exc
    if not isinstance(data, list) or not data or len(data) >= 1000:
        raise CommunicationPolicyError('complete member evidence exceeds cap or is unavailable')
    members = {}
    for raw in data:
        user = raw.get('user') if isinstance(raw, dict) else None
        roles = raw.get('roles') if isinstance(raw, dict) else None
        if not isinstance(user, dict) or not isinstance(roles, list):
            raise CommunicationPolicyError('incomplete raw member evidence')
        uid = str(user.get('id', ''))
        member_roles = frozenset(str(rid) for rid in roles)
        if not uid.isdecimal() or uid in members or not member_roles <= role_ids:
            raise CommunicationPolicyError('stale or ambiguous raw member role evidence')
        members[uid] = (member_roles, user.get('bot') is True)
    if str(owner_id) not in members:
        raise CommunicationPolicyError('owner absent from complete member evidence')
    return members


async def _restricted_reader_scope(client, channel, policy):
    """Prove owner/sender readers plus explicitly recorded, current guild admin bots.

    Existing admin bot IDs grant no permissions: every call requires fresh managed
    bot identity tags plus administrator evidence. A managed admin role proves it
    directly; a shared guild Admin role additionally needs complete fresh raw member
    role IDs. Unmanaged reader roles require proof that all actual holders are already
    allowed. No cached members or permission names prove these facts. User-approved
    shared participants need current managed bot tags and are admitted only on the
    three primary text surfaces, including the parent of a public child thread.
    This opt-in does not authorize those bots on private output channels or in voice.
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
    if default_role is None or getattr(default_role.permissions, 'administrator', False):
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
    identity_roles = {}
    for role in roles:
        identity = managed_bot_id(role)
        if identity:
            identity_roles.setdefault(identity, set()).add(str(role.id))
    members = None
    unresolved_admin_bots = policy.existing_admin_bot_ids - verified_admin_bots
    if unresolved_admin_bots:
        if not unresolved_admin_bots <= identity_roles.keys():
            return False
        members = await _fresh_member_roles(client, guild_id, {str(rid) for rid in role_ids}, guild.owner_id)
        roles_by_id = {str(role.id): role for role in roles}
        for uid in unresolved_admin_bots:
            member_roles, is_bot = members.get(uid, (frozenset(), False))
            if (not is_bot or not member_roles & identity_roles[uid]
                    or not any(getattr(roles_by_id[rid].permissions, 'administrator', False)
                               for rid in member_roles)):
                return False
        verified_admin_bots |= unresolved_admin_bots
    shared_surface = (str(getattr(channel, 'id', '')) in policy.primary_channels.values()
                      and _kind(channel) == 0)
    verified_shared_bots = {
        managed_bot_id(role) for role in roles
        if shared_surface and managed_bot_id(role) in policy.shared_participant_bot_ids
    }
    if shared_surface and policy.shared_participant_bot_ids - verified_shared_bots:
        return False
    allowed_bot_ids = verified_admin_bots | verified_shared_bots | {bot_id}
    everyone_base_view = bool(getattr(default_role.permissions, 'view_channel', False))
    reader_roles_needing_members = set()
    for role in roles:
        trusted_bot_role = managed_bot_id(role) in allowed_bot_ids
        if getattr(role.permissions, 'administrator', False) and not trusted_bot_role:
            reader_roles_needing_members.add(str(role.id))
        # Discord ORs guild role base permissions, then applies @everyone overwrite,
        # aggregate role denies and aggregate role allows. Testing every non-bot role
        # plus rejecting every effective role allow proves no combination adds VIEW.
        base_view = everyone_base_view or bool(getattr(role.permissions, 'view_channel', False))
        after_everyone = (base_view and not default_deny & view) or bool(default_allow & view)
        allow, deny = overwrite_by_role.get(int(role.id), (0, 0))
        effective_view = (after_everyone and not deny & view) or bool(allow & view)
        if effective_view and not trusted_bot_role:
            reader_roles_needing_members.add(str(role.id))
    allowed_members = policy.owner_ids | allowed_bot_ids
    if any(kind == 1 and allow & view and str(oid) not in allowed_members
           for oid, kind, allow, _ in entries):
        return False
    if reader_roles_needing_members:
        if members is None:
            members = await _fresh_member_roles(client, guild_id, {str(rid) for rid in role_ids}, guild.owner_id)
        for uid in allowed_bot_ids:
            member_roles, is_bot = members.get(uid, (frozenset(), False))
            if not is_bot or not member_roles & identity_roles.get(uid, set()):
                return False
        for rid in reader_roles_needing_members:
            if any(uid not in allowed_members and rid in member_roles
                   for uid, (member_roles, _) in members.items()):
                return False
    return True



async def resolve_discord_delivery(client, policy, chat_id, metadata=None, *, editing=False):
    """Resolve only the delivery reference and prove its audience before any payload is sent.

    ``communication_source_channel_id`` is an existing trusted source boundary, not a
    user/role allowlist. Direct destinations require verified restricted readers; optional
    existing admin bots must have current managed identity and administrator evidence
    in this guild; shared roles additionally require complete fresh raw membership.
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
    try:
        return await _restricted_reader_scope(client, voice, policy)
    except CommunicationPolicyError:
        return False  # Incomplete membership evidence never authorizes voice playback.
