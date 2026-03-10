from discord import app_commands

# Root command group: /ic
ic = app_commands.Group(name="ic", description="Instigate Cafe utilities")

# Sub-groups
casino = app_commands.Group(name="casino", description="Casino games", parent=ic)
icse = app_commands.Group(name="icse", description="Instigate Cafe Stock Exchange", parent=ic)
admin = app_commands.Group(name="admin", description="Admin commands", parent=ic)
exam = app_commands.Group(name="exam", description="Exam schedule", parent=ic)
sports = app_commands.Group(name="sports", description="Sports updates", parent=ic)
