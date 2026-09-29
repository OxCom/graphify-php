"""Measurement harness for the PHP call-target resolvers.

Nothing here imports `graphify_php`. The harness has to be able to judge a resolver that does
not exist yet, and one that is written in a different language and hands over JSON; a scorer
that imports the thing it measures cannot do either.
"""
