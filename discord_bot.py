import asyncio
import json
import os
import re
import time
from typing import Optional

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands, tasks

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

DISCORD_BOT_TOKEN = os.environ.get("DISCORD_BOT_TOKEN", "")
ANNOUNCE_CHANNEL_ID = int(os.environ.get("ANNOUNCE_CHANNEL_ID", "1518363017836888244"))

GRAPHQL_ACCESS_TOKEN = os.environ.get("GRAPHQL_ACCESS_TOKEN", "")
APP_ID = 1614607450665189
DOCID = 6771539532935162
GAME_NAME = "Blade City"
DEVELOPER_NAME = os.environ.get("DEVELOPER_NAME", "Unknown Developer")

EMBED_COLOR = discord.Color.from_rgb(54, 57, 63)  # grey

POLL_INTERVAL_SECONDS = 300  # 5 minutes
STATE_FILE = "version_state.json"

# --- Team system config ---
TEAM_APPROVAL_CHANNEL_ID = 1518372067475456021
TEAM_LEADER_LOG_CHANNEL_ID = 1518372889861030039
TEAM_CATEGORY_ID = 1518372256395427931
TEAM_DB_CHANNEL_ID = 1518373722350817391

# ---------------------------------------------------------------------------
# GraphQL client (same logic as before)
# ---------------------------------------------------------------------------


class GraphQLClient:
    def __init__(
        self,
        url: str = "https://graph.oculus.com/graphql",
        max_requests: int = 5,
        per_seconds: float = 5.0,
    ) -> None:
        self.url = url
        self.max_requests = max_requests
        self.per_seconds = per_seconds
        self._timestamps: list[float] = []
        self._session: Optional[aiohttp.ClientSession] = None
        self._timeout = aiohttp.ClientTimeout(total=15)

    async def _acquire_slot(self) -> None:
        now = asyncio.get_running_loop().time()
        self._timestamps = [t for t in self._timestamps if now - t < self.per_seconds]

        if len(self._timestamps) >= self.max_requests:
            delay = self.per_seconds - (now - self._timestamps[0])
            if delay > 0:
                await asyncio.sleep(delay)

        self._timestamps.append(asyncio.get_running_loop().time())

    async def post(self, payload: dict) -> Optional[dict]:
        await self._acquire_slot()

        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=self._timeout)

        try:
            async with self._session.post(self.url, data=payload) as resp:
                if resp.status >= 400:
                    body = await resp.text()
                    print(f"GraphQL error {resp.status}: {body[:500]}")
                    return None
                return await resp.json(content_type=None)
        except Exception as e:
            print(f"GraphQL error: {type(e).__name__}: {e}")
            if self._session and not self._session.closed:
                await self._session.close()
            self._session = None
            return None

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()


graphql_client = GraphQLClient()


def _payload() -> dict:
    return {
        "access_token": GRAPHQL_ACCESS_TOKEN,
        "variables": json.dumps({"applicationID": str(APP_ID)}),
        "doc_id": str(DOCID),
    }


async def fetch_store_metadata() -> Optional[dict]:
    data = await graphql_client.post(_payload())
    return data if isinstance(data, dict) else None


async def get_live_version(meta: Optional[dict] = None) -> Optional[str]:
    if meta is None:
        meta = await fetch_store_metadata()
    if not isinstance(meta, dict):
        return None
    nodes = meta.get("data", {}).get("node", {}).get("liveChannel", {}).get("nodes", [])
    if not nodes:
        return None
    return nodes[0].get("latest_supported_binary", {}).get("version")


async def get_dev_version(meta: Optional[dict] = None) -> Optional[str]:
    if meta is None:
        meta = await fetch_store_metadata()
    if not isinstance(meta, dict):
        return None
    nodes = meta.get("data", {}).get("node", {}).get("primary_binaries", {}).get("nodes", [])
    if not nodes:
        return None
    return nodes[0].get("version")


def _find_image_url(obj) -> Optional[str]:
    """Best-effort recursive search for an image URL in the GraphQL response.
    Looks for common keys (uri/url) nested under image-ish field names.
    Falls back to GAME_BANNER_URL env var if nothing is found."""
    if isinstance(obj, dict):
        for key, value in obj.items():
            if isinstance(value, dict) and "uri" in value and isinstance(value["uri"], str):
                if any(tag in key.lower() for tag in ("image", "banner", "hero", "cover", "screenshot")):
                    return value["uri"]
            found = _find_image_url(value)
            if found:
                return found
    elif isinstance(obj, list):
        for item in obj:
            found = _find_image_url(item)
            if found:
                return found
    return None


async def get_banner_url(meta: Optional[dict] = None) -> Optional[str]:
    """Fetch the game's banner image from its actual store page (og:image meta tag),
    falling back to scanning the GraphQL response, then to GAME_BANNER_URL env var."""
    store_url = f"https://www.meta.com/experiences/{APP_ID}/"
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as session:
            async with session.get(store_url, headers={"User-Agent": "Mozilla/5.0"}) as resp:
                if resp.status == 200:
                    html = await resp.text()
                    match = re.search(
                        r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)["\']',
                        html,
                        re.IGNORECASE,
                    )
                    if match:
                        return match.group(1)
    except Exception as e:
        print(f"Banner fetch error: {type(e).__name__}: {e}")

    if meta is None:
        meta = await fetch_store_metadata()
    if isinstance(meta, dict):
        found = _find_image_url(meta)
        if found:
            return found

    return os.environ.get("GAME_BANNER_URL")


# ---------------------------------------------------------------------------
# State persistence
# ---------------------------------------------------------------------------


def load_state() -> dict:
    try:
        with open(STATE_FILE, "r") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_state(state: dict) -> None:
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


# ---------------------------------------------------------------------------
# Discord bot
# ---------------------------------------------------------------------------

intents = discord.Intents.default()
intents.members = True
bot = commands.Bot(command_prefix="!", intents=intents)

state = load_state()


# ---------------------------------------------------------------------------
# Team system: persistence (stored as a JSON blob in a Discord channel,
# since the host's filesystem isn't guaranteed to persist across deploys)
# ---------------------------------------------------------------------------

teams: dict = {}  # { team_name: {leader_id, members: [...], role_id, channel_id} }
_teams_db_message: Optional[discord.Message] = None


async def load_teams_from_db() -> None:
    """Scan the DB channel for the bot's existing state message and load it.
    If none exists yet, create one with an empty team dict."""
    global _teams_db_message, teams

    channel = bot.get_channel(TEAM_DB_CHANNEL_ID)
    if channel is None:
        print(f"WARNING: team DB channel {TEAM_DB_CHANNEL_ID} not found - team data won't persist.")
        return

    async for msg in channel.history(limit=100):
        if msg.author.id == bot.user.id and "```json" in msg.content:
            raw = msg.content.split("```json", 1)[1].rsplit("```", 1)[0].strip()
            try:
                teams = json.loads(raw)
            except json.JSONDecodeError:
                teams = {}
            _teams_db_message = msg
            print(f"Loaded {len(teams)} team(s) from DB channel.")
            return

    teams = {}
    _teams_db_message = await channel.send(f"```json\n{json.dumps(teams, indent=2)}\n```")
    print("No existing team DB message found - created a new one.")


async def save_teams_to_db() -> None:
    """Persist the current `teams` dict by editing the bot's DB message in place."""
    global _teams_db_message

    channel = bot.get_channel(TEAM_DB_CHANNEL_ID)
    if channel is None:
        print(f"WARNING: team DB channel {TEAM_DB_CHANNEL_ID} not found - could not save team data.")
        return

    content = f"```json\n{json.dumps(teams, indent=2)}\n```"
    if len(content) > 1990:
        print("WARNING: team DB content is approaching Discord's 2000-char message limit. "
              "Consider migrating to a real database if you have many teams.")

    if _teams_db_message is None:
        _teams_db_message = await channel.send(content)
        return

    try:
        await _teams_db_message.edit(content=content)
    except discord.NotFound:
        _teams_db_message = await channel.send(content)


def find_team_by_leader(user_id: int) -> Optional[str]:
    for name, t in teams.items():
        if t["leader_id"] == user_id:
            return name
    return None


def find_team_by_member(user_id: int) -> Optional[str]:
    for name, t in teams.items():
        if user_id in t.get("members", []):
            return name
    return None


async def reattach_pending_views() -> None:
    """After a restart, Discord drops in-memory view state. Re-register a
    live view (bound to its original custom_id) for any team-creation request
    in the approval channel that hasn't been resolved yet, so old buttons
    keep working. (Pending /inviteuser invites, which can be posted in any
    channel, aren't re-scanned - if the bot restarts mid-invite, ask the
    leader to resend it.)"""
    channel = bot.get_channel(TEAM_APPROVAL_CHANNEL_ID)
    if channel is None:
        return

    count = 0
    async for msg in channel.history(limit=200):
        if msg.author.id != bot.user.id or not msg.components:
            continue
        if msg.embeds and any(f.name == "Status" for f in msg.embeds[0].fields):
            continue  # already resolved

        try:
            buttons = msg.components[0].children
            accept_custom_id = next(c.custom_id for c in buttons if "accept" in c.custom_id)
            team_name, leader_id = TeamCreationView._parse(accept_custom_id)
        except (StopIteration, ValueError, IndexError):
            continue

        bot.add_view(TeamCreationView(team_name, leader_id), message_id=msg.id)
        count += 1

    if count:
        print(f"Re-attached {count} pending team-creation view(s).")


@bot.event
async def on_ready():
    print(f"Logged in as {bot.user} (id={bot.user.id})")
    await load_teams_from_db()
    await reattach_pending_views()
    try:
        synced = await bot.tree.sync()
        print(f"Synced {len(synced)} slash command(s)")
    except Exception as e:
        print(f"Slash command sync failed: {e}")
    if not version_check_loop.is_running():
        version_check_loop.start()


def build_update_embed(old_version: Optional[str], new_version: str, banner_url: Optional[str]) -> discord.Embed:
    embed = discord.Embed(
        title="Update Detected!",
        description=f"**{DEVELOPER_NAME}, {GAME_NAME}**",
        color=EMBED_COLOR,
        timestamp=discord.utils.utcnow(),
    )
    embed.add_field(
        name="🟢 | Updated Version:",
        value=f"```{new_version}```",
        inline=False,
    )
    embed.add_field(
        name="🔴 | Last Logged:",
        value=f"```{old_version or 'None'}```",
        inline=False,
    )
    if banner_url:
        embed.set_image(url=banner_url)
    return embed


@tasks.loop(seconds=POLL_INTERVAL_SECONDS)
async def version_check_loop():
    global state

    meta = await fetch_store_metadata()
    live = await get_live_version(meta)
    dev = await get_dev_version(meta)

    changed_version = None
    old_version = None

    if live is not None and live != state.get("live"):
        old_version = state.get("live")
        state["live"] = live
        changed_version = live
    if dev is not None and dev != state.get("dev"):
        if changed_version is None:
            old_version = state.get("dev")
            changed_version = dev
        state["dev"] = dev

    if changed_version:
        save_state(state)
        banner_url = await get_banner_url(meta)
        channel = bot.get_channel(ANNOUNCE_CHANNEL_ID)
        if channel is not None:
            embed = build_update_embed(old_version, changed_version, banner_url)
            await channel.send(embed=embed)
        else:
            print("ANNOUNCE_CHANNEL_ID not found / bot lacks access:", ANNOUNCE_CHANNEL_ID)
    else:
        print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] No change (live={live}, dev={dev})")


@version_check_loop.before_loop
async def before_loop():
    await bot.wait_until_ready()


@bot.tree.command(name="test", description="Post a sample update-detected embed to the announce channel")
async def test_cmd(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)

    channel = bot.get_channel(ANNOUNCE_CHANNEL_ID)
    if channel is None:
        await interaction.followup.send(
            f"Couldn't find channel `{ANNOUNCE_CHANNEL_ID}` — check ANNOUNCE_CHANNEL_ID and bot permissions.",
            ephemeral=True,
        )
        return

    meta = await fetch_store_metadata()
    banner_url = await get_banner_url(meta)
    embed = build_update_embed(
        old_version=state.get("live") or state.get("dev") or "1.0.0.0000",
        new_version="1.0.0.0001",
        banner_url=banner_url,
    )
    await channel.send(embed=embed)
    await interaction.followup.send(f"Test embed posted in <#{ANNOUNCE_CHANNEL_ID}>.", ephemeral=True)


@bot.command(name="version")
async def version_cmd(ctx: commands.Context):
    """Manually check the current live/dev versions."""
    meta = await fetch_store_metadata()
    live = await get_live_version(meta)
    dev = await get_dev_version(meta)
    await ctx.send(f"App `{APP_ID}`\nLive: `{live}`\nDev: `{dev}`")


@bot.command(name="trackstatus")
async def trackstatus_cmd(ctx: commands.Context):
    """Show last-known tracked versions and poll interval."""
    channel_line = f"Announce channel: <#{ANNOUNCE_CHANNEL_ID}>" if ANNOUNCE_CHANNEL_ID else "Announce channel: not set"
    await ctx.send(
        f"Tracking app `{APP_ID}` ({GAME_NAME})\n"
        f"Last known live: `{state.get('live')}`\n"
        f"Last known dev: `{state.get('dev')}`\n"
        f"Poll interval: {POLL_INTERVAL_SECONDS}s\n"
        f"{channel_line}"
    )


# ---------------------------------------------------------------------------
# Team system: UI views
# ---------------------------------------------------------------------------


class TeamCreationView(discord.ui.View):
    """Approve/decline a team-creation request.
    State (team name + leader id) is encoded in each button's custom_id so the
    buttons keep working correctly even after a bot restart, when a fresh
    View instance with no constructor args gets matched to old messages."""

    def __init__(self, team_name: str = "", leader_id: int = 0):
        super().__init__(timeout=None)
        self.accept_btn.custom_id = f"team_create_accept|{team_name}|{leader_id}"
        self.decline_btn.custom_id = f"team_create_decline|{team_name}|{leader_id}"

    @staticmethod
    def _parse(custom_id: str) -> tuple[str, int]:
        _, team_name, leader_id = custom_id.split("|", 2)
        return team_name, int(leader_id)

    async def _disable_and_label(self, interaction: discord.Interaction, label: str):
        for child in self.children:
            child.disabled = True
        embed = interaction.message.embeds[0]
        embed.add_field(name="Status", value=label, inline=False)
        await interaction.response.edit_message(embed=embed, view=self)

    @discord.ui.button(label="Accept", style=discord.ButtonStyle.green, custom_id="team_create_accept|placeholder|0")
    async def accept_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        team_name, leader_id = self._parse(button.custom_id)

        if not interaction.user.guild_permissions.manage_roles:
            await interaction.response.send_message(
                "You need the Manage Roles permission to approve team requests.", ephemeral=True
            )
            return

        if team_name in teams:
            await interaction.response.send_message("That team already exists.", ephemeral=True)
            return

        guild = interaction.guild
        leader = guild.get_member(leader_id)
        if leader is None:
            await interaction.response.send_message("The requesting user is no longer in the server.", ephemeral=True)
            return

        role = await guild.create_role(name=f"{team_name} Team", reason=f"Team approved by {interaction.user}")
        await leader.add_roles(role, reason="Team leader")

        category = guild.get_channel(TEAM_CATEGORY_ID)
        overwrites = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
            role: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True),
        }
        channel = await guild.create_text_channel(
            name=f"🏆┃{team_name}-discussion",
            category=category,
            overwrites=overwrites,
            reason=f"Team channel for {team_name}",
        )

        teams[team_name] = {
            "leader_id": leader_id,
            "members": [leader_id],
            "role_id": role.id,
            "channel_id": channel.id,
        }
        await save_teams_to_db()

        log_channel = bot.get_channel(TEAM_LEADER_LOG_CHANNEL_ID)
        if log_channel is not None:
            await log_channel.send(
                f"👑 {leader.mention} is now leader of **{team_name}** "
                f"(role: {role.mention}, channel: {channel.mention})"
            )

        await self._disable_and_label(interaction, f"✅ Approved by {interaction.user.mention}")

    @discord.ui.button(label="Decline", style=discord.ButtonStyle.red, custom_id="team_create_decline|placeholder|0")
    async def decline_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not interaction.user.guild_permissions.manage_roles:
            await interaction.response.send_message(
                "You need the Manage Roles permission to decline team requests.", ephemeral=True
            )
            return
        await self._disable_and_label(interaction, f"❌ Declined by {interaction.user.mention}")


class TeamInviteView(discord.ui.View):
    """Accept/decline a team invite. State encoded in custom_id, same reasoning as above."""

    def __init__(self, team_name: str = "", invited_user_id: int = 0):
        super().__init__(timeout=None)
        self.accept_btn.custom_id = f"team_invite_accept|{team_name}|{invited_user_id}"
        self.decline_btn.custom_id = f"team_invite_decline|{team_name}|{invited_user_id}"

    @staticmethod
    def _parse(custom_id: str) -> tuple[str, int]:
        _, team_name, invited_user_id = custom_id.split("|", 2)
        return team_name, int(invited_user_id)

    async def _disable_and_label(self, interaction: discord.Interaction, label: str):
        for child in self.children:
            child.disabled = True
        embed = interaction.message.embeds[0]
        embed.add_field(name="Status", value=label, inline=False)
        await interaction.response.edit_message(embed=embed, view=self)

    @discord.ui.button(label="Accept", style=discord.ButtonStyle.green, custom_id="team_invite_accept|placeholder|0")
    async def accept_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        team_name, invited_user_id = self._parse(button.custom_id)

        if interaction.user.id != invited_user_id:
            await interaction.response.send_message("This invite isn't addressed to you.", ephemeral=True)
            return

        team = teams.get(team_name)
        if team is None:
            await interaction.response.send_message("That team no longer exists.", ephemeral=True)
            return

        guild = interaction.guild
        role = guild.get_role(team["role_id"])
        member = guild.get_member(invited_user_id)

        if role is not None and member is not None:
            await member.add_roles(role, reason=f"Joined team {team_name}")

        if invited_user_id not in team["members"]:
            team["members"].append(invited_user_id)
            await save_teams_to_db()

        await self._disable_and_label(interaction, f"✅ {member.mention if member else 'User'} joined the team")

    @discord.ui.button(label="Decline", style=discord.ButtonStyle.red, custom_id="team_invite_decline|placeholder|0")
    async def decline_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        _, invited_user_id = self._parse(button.custom_id)
        if interaction.user.id != invited_user_id:
            await interaction.response.send_message("This invite isn't addressed to you.", ephemeral=True)
            return
        await self._disable_and_label(interaction, f"❌ {interaction.user.mention} declined")


# ---------------------------------------------------------------------------
# Team system: slash commands
# ---------------------------------------------------------------------------


@bot.tree.command(name="createteam", description="Request to create a new team")
@app_commands.describe(name="The name of the team")
async def createteam_cmd(interaction: discord.Interaction, name: str):
    if name in teams:
        await interaction.response.send_message("A team with that name already exists.", ephemeral=True)
        return

    approval_channel = bot.get_channel(TEAM_APPROVAL_CHANNEL_ID)
    if approval_channel is None:
        await interaction.response.send_message(
            "Couldn't find the team approval channel - contact an admin.", ephemeral=True
        )
        return

    embed = discord.Embed(
        title="Team Creation Request",
        description=f"{interaction.user.mention} wants to create the team **{name}**.",
        color=EMBED_COLOR,
        timestamp=discord.utils.utcnow(),
    )
    embed.set_footer(text=f"Requested by {interaction.user}")

    view = TeamCreationView(name, interaction.user.id)
    await approval_channel.send(embed=embed, view=view)
    await interaction.response.send_message(
        f"Your request to create **{name}** has been sent to <#{TEAM_APPROVAL_CHANNEL_ID}> for approval.",
        ephemeral=True,
    )


@bot.tree.command(name="inviteuser", description="Invite a user to your team (team leaders only)")
@app_commands.describe(user="The user to invite")
async def inviteuser_cmd(interaction: discord.Interaction, user: discord.Member):
    team_name = find_team_by_leader(interaction.user.id)
    if team_name is None:
        await interaction.response.send_message("You're not the leader of any team.", ephemeral=True)
        return

    team = teams[team_name]
    if user.id in team["members"]:
        await interaction.response.send_message(f"{user.mention} is already in **{team_name}**.", ephemeral=True)
        return

    if user.bot:
        await interaction.response.send_message("You can't invite bots to a team.", ephemeral=True)
        return

    embed = discord.Embed(
        title="Team Invite",
        description=f"{user.mention}, you've been invited to join **{team_name}** by {interaction.user.mention}.",
        color=EMBED_COLOR,
        timestamp=discord.utils.utcnow(),
    )
    view = TeamInviteView(team_name, user.id)
    await interaction.response.send_message(embed=embed, view=view)


if __name__ == "__main__":
    if not DISCORD_BOT_TOKEN:
        raise SystemExit("Set the DISCORD_BOT_TOKEN environment variable before running.")
    if not GRAPHQL_ACCESS_TOKEN or GRAPHQL_ACCESS_TOKEN.endswith("|"):
        print("WARNING: GRAPHQL_ACCESS_TOKEN is missing or looks incomplete (format should be "
              "OC|<app_id>|<app_secret>) - GraphQL requests will fail with 400 until this is fixed.")
    if not ANNOUNCE_CHANNEL_ID:
        print("WARNING: ANNOUNCE_CHANNEL_ID not set - bot will not post announcements, "
              "only respond to !version / !trackstatus commands.")
    bot.run(DISCORD_BOT_TOKEN)
