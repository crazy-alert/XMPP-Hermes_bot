def register(ctx):
    def build_adapter(config=None):
        import slixmpp
        return {"context": ctx, "config": config}
    return build_adapter
