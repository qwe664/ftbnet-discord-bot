import discord


class DangerousConfirmView(discord.ui.View):
    def __init__(self, author, command_text, callback):
        super().__init__(timeout=30)

        self.author = author
        self.command_text = command_text
        self.callback = callback

    @discord.ui.button(
        label="確認執行",
        style=discord.ButtonStyle.danger,
        emoji="⚠️"
    )
    async def confirm(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button
    ):
        if interaction.user.id != self.author.id:
            await interaction.response.send_message(
                "❌ 只有指令發送者可以操作此按鈕。",
                ephemeral=True
            )
            return

        await interaction.response.defer()

        try:
            print("① 開始執行 callback")
            await self.callback(self.command_text)
            print("② callback 執行完成")

            await interaction.message.edit(
                content=f"✅ 已執行危險指令：`{self.command_text}`",
                view=None
            )
            print("③ 訊息編輯完成")

        except Exception as e:
            print("confirm error:", e)
    
    @discord.ui.button(
        label="取消",
        style=discord.ButtonStyle.secondary,
        emoji="❌"
    )
    async def cancel(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button
    ):
        if interaction.user.id != self.author.id:
            await interaction.response.send_message(
                "❌ 只有指令發送者可以操作此按鈕。",
                ephemeral=True
            )
            return

        await interaction.response.edit_message(
            content="❌ 已取消執行。",
            view=None
        )