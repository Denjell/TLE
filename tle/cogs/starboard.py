import asyncio
import logging

import discord
from discord import app_commands
from discord.ext import commands

from tle import constants
from tle.util import codeforces_common as cf_common
from tle.util import discord_common

_STAR = '\N{WHITE MEDIUM STAR}'
_STAR_ORANGE = 0xffaa10
_STAR_THRESHOLD = 5


class StarboardCogError(commands.CommandError):
    pass


class Starboard(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.locks = {}
        self.logger = logging.getLogger(self.__class__.__name__)
        self.ctx_menu = app_commands.ContextMenu(
            name='Add to Starboard',
            callback=self.starboard_context_menu,
        )
        self.bot.tree.add_command(self.ctx_menu)

    async def cog_unload(self):
        self.bot.tree.remove_command(self.ctx_menu.name, type=self.ctx_menu.type)

    @app_commands.guild_only()
    async def starboard_context_menu(self, interaction: discord.Interaction,
                                     message: discord.Message):
        await interaction.response.defer(ephemeral=True)
        res = cf_common.user_db.get_starboard(interaction.guild_id)
        if res is None:
            await interaction.followup.send(
                embed=discord_common.embed_alert('Starboard is not set up in this server.'),
                ephemeral=True,
            )
            return

        starboard_channel_id = int(res[0])
        try:
            fetched = await message.channel.fetch_message(message.id)
            added = await self.check_and_add_to_starboard(
                starboard_channel_id, message, interaction.user.id,
                reactions=fetched.reactions,
            )
        except StarboardCogError as e:
            await interaction.followup.send(
                embed=discord_common.embed_alert(str(e)),
                ephemeral=True,
            )
            return

        if added:
            await interaction.followup.send(
                embed=discord_common.embed_success('Added message to starboard!'),
                ephemeral=True,
            )
        else:
            await interaction.followup.send(
                embed=discord_common.embed_alert(
                    f'Message needs at least {_STAR_THRESHOLD} {_STAR} reactions '
                    'or is already on the starboard.'
                ),
                ephemeral=True,
            )

    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload):
        if str(payload.emoji) != _STAR or payload.guild_id is None:
            return
        res = cf_common.user_db.get_starboard(payload.guild_id)
        if res is None:
            return
        starboard_channel_id = int(res[0])
        channel = self.bot.get_channel(payload.channel_id)
        if channel is None:
            return
        try:
            message = await channel.fetch_message(payload.message_id)
            await self.check_and_add_to_starboard(
                starboard_channel_id, message, payload.user_id,
            )
        except StarboardCogError as e:
            self.logger.info(f'Failed to starboard: {e!r}')

    @commands.Cog.listener()
    async def on_raw_message_delete(self, payload):
        if payload.guild_id is None:
            return
        res = cf_common.user_db.get_starboard(payload.guild_id)
        if res is None:
            return
        starboard_channel_id = int(res[0])
        if payload.channel_id != starboard_channel_id:
            return
        cf_common.user_db.remove_starboard_message(starboard_msg_id=payload.message_id)
        self.logger.info(f'Removed message {payload.message_id} from starboard')

    @staticmethod
    def prepare_embed(message):
        # Adapted from https://github.com/Rapptz/RoboDanny/blob/rewrite/cogs/stars.py
        embed = discord.Embed(color=_STAR_ORANGE, timestamp=message.created_at)
        embed.add_field(name='Channel', value=message.channel.mention)
        embed.add_field(name='Jump to', value=f'[Original]({message.jump_url})')

        if message.content:
            embed.add_field(name='Content', value=message.content, inline=False)

        if message.embeds:
            data = message.embeds[0]
            if data.type == 'image':
                embed.set_image(url=data.url)

        if message.attachments:
            file = message.attachments[0]
            if file.filename.lower().endswith(('png', 'jpeg', 'jpg', 'gif', 'webp')):
                embed.set_image(url=file.url)
            else:
                embed.add_field(name='Attachment', value=f'[{file.filename}]({file.url})', inline=False)

        embed.set_footer(text=str(message.author), icon_url=message.author.avatar)
        return embed

    async def check_and_add_to_starboard(self, starboard_channel_id, message,
                                         user_id, reactions=None):
        guild = message.guild
        starboard_channel = guild.get_channel(starboard_channel_id)
        if starboard_channel is None:
            raise StarboardCogError('Starboard channel not found')

        if ((message.type != discord.MessageType.default and message.type != discord.MessageType.reply)
            or (len(message.content) == 0 and len(message.attachments) == 0)):
            raise StarboardCogError('Cannot starboard this message')

        reactions = message.reactions if reactions is None else reactions
        reaction_count = sum(reaction.count for reaction in reactions
                             if str(reaction) == _STAR)
        if reaction_count < _STAR_THRESHOLD:
            return False
        lock = self.locks.get(guild.id)
        if lock is None:
            self.locks[guild.id] = lock = asyncio.Lock()

        async with lock:
            if cf_common.user_db.check_exists_starboard_message(message.id):
                return False
            embed = self.prepare_embed(message)
            starboard_message = await starboard_channel.send(embed=embed)
            cf_common.user_db.add_starboard_message(message.id, starboard_message.id, guild.id)
            self.logger.info(f'Added message {message.id} to starboard (Triggered by {user_id})')
            return True

    @commands.hybrid_group(brief='Starboard commands',
                           invoke_without_command=True)
    async def starboard(self, ctx):
        """Group for commands involving the starboard."""
        await ctx.send_help(ctx.command)

    @starboard.command(brief='Set starboard to current channel')
    @commands.has_role(constants.TLE_ADMIN)
    async def here(self, ctx):
        """Set the current channel as starboard."""
        res = cf_common.user_db.get_starboard(ctx.guild.id)
        if res is not None:
            raise StarboardCogError('The starboard channel is already set. Use `clear` before '
                                    'attempting to set a different channel as starboard.')
        cf_common.user_db.set_starboard(ctx.guild.id, ctx.channel.id)
        await ctx.send(embed=discord_common.embed_success('Starboard channel set'))

    @starboard.command(brief='Clear starboard settings')
    @commands.has_role(constants.TLE_ADMIN)
    async def clear(self, ctx):
        """Stop tracking starboard messages and remove the currently set starboard channel
        from settings."""
        cf_common.user_db.clear_starboard(ctx.guild.id)
        cf_common.user_db.clear_starboard_messages_for_guild(ctx.guild.id)
        await ctx.send(embed=discord_common.embed_success('Starboard channel cleared'))

    @starboard.command(brief='Remove a message from starboard')
    @commands.has_role(constants.TLE_ADMIN)
    async def remove(self, ctx, original_message_id: str):
        """Remove a particular message from the starboard database."""
        try:
            msg_id = int(original_message_id)
        except ValueError:
            raise StarboardCogError('Invalid message ID.')
        rc = cf_common.user_db.remove_starboard_message(original_msg_id=msg_id)
        if rc:
            await ctx.send(embed=discord_common.embed_success('Successfully removed'))
        else:
            await ctx.send(embed=discord_common.embed_alert('Not found in database'))

    @discord_common.send_error_if(StarboardCogError)
    async def cog_command_error(self, ctx, error):
        pass


async def setup(bot):
    await bot.add_cog(Starboard(bot))