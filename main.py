class PlayerCountModal(discord.ui.Modal, title="Select Number of Players"):
    def __init__(self):
        super().__init__()
        self.player_count = discord.ui.TextInput(
            label="Number of players (4, 8, 16...)", placeholder="4, 8, 16...", max_length=4
        )
        self.add_item(self.player_count)

    async def on_submit(self, interaction: discord.Interaction):
        try:
            count = int(self.player_count.value.strip())
            if count < 4 or count > 64 or count % 2 != 0:
                raise ValueError()
        except ValueError:
            await interaction.response.send_message("❌ Invalid number. Must be even and 4–64.", ephemeral=True)
            return

        # ✅ Proper manager
        manager = TournamentManager(creator=interaction.user.id, max_players=count)
        embed = await manager.build_embed(interaction.guild)
        manager.message = await interaction.channel.send(embed=embed, view=manager)

        await interaction.response.send_message(
            f"✅ Tournament lobby created for **{count}** players!",
            ephemeral=True
        )
