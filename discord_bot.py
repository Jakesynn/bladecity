import asyncio
import json
import os
import re
import time
from typing import Optional

import aiohttp
import discord
from discord.ext import commands, tasks

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

DISCORD_BOT_TOKEN = os.environ.get("DISCORD_BOT_TOKEN", "")
ANNOUNCE_CHANNEL_ID = int(os.environ.get("ANNOUNCE_CHANNEL_ID", "1518363017836888244"))

GRAPHQL_ACCESS_TOKEN = "OC|752908224809889|"
APP_ID = 1614607450665189
DOCID = 6771539532935162
GAME_NAME = "Blade City"
DEVELOPER_NAME = os.environ.get("DEVELOPER_NAME", "Unknown Developer")

EMBED_COLOR = discord.Color.from_rgb(54, 57, 63)  # grey

POLL_INTERVAL_SECONDS = 300  # 5 minutes
STATE_FILE = "version_state.json"

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
                resp.raise_for_status()
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
bot = commands.Bot(command_prefix="!", intents=intents)

state = load_state()


@bot.event
async def on_ready():
    print(f"Logged in as {bot.user} (id={bot.user.id})")
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


if __name__ == "__main__":
    if not DISCORD_BOT_TOKEN:
        raise SystemExit("Set the DISCORD_BOT_TOKEN environment variable before running.")
    if not ANNOUNCE_CHANNEL_ID:
        print("WARNING: ANNOUNCE_CHANNEL_ID not set - bot will not post announcements, "
              "only respond to !version / !trackstatus commands.")
    bot.run(DISCORD_BOT_TOKEN)
