"""Conversation thread reconstruction utilities."""
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from typing import Dict, List, Optional, Set, Tuple


@dataclass
class TweetData:
    """Lightweight tweet data for thread building."""

    id: int
    author_id: str
    inbound: bool
    created_at: datetime
    text: str
    parent_id: Optional[int] = None


@dataclass
class ConversationThread:
    """Reconstructed conversation thread."""

    root_id: int
    tweets: List[TweetData]
    participants: Set[str]
    start_time: datetime
    end_time: datetime

    @property
    def message_count(self) -> int:
        return len(self.tweets)

    @property
    def participant_count(self) -> int:
        return len(self.participants)


class ThreadBuilder:
    """Utility for reconstructing conversation threads from tweets."""

    def __init__(self):
        self._tweets: Dict[int, TweetData] = {}
        self._children: Dict[int, List[int]] = defaultdict(list)
        self._roots: Set[int] = set()

    def add_tweet(
        self,
        tweet_id: int,
        author_id: str,
        inbound: bool,
        created_at: datetime,
        text: str,
        parent_id: Optional[int] = None,
    ) -> None:
        """Add a tweet to the builder."""
        tweet = TweetData(
            id=tweet_id,
            author_id=author_id,
            inbound=inbound,
            created_at=created_at,
            text=text,
            parent_id=parent_id,
        )
        self._tweets[tweet_id] = tweet

        if parent_id:
            self._children[parent_id].append(tweet_id)
        else:
            self._roots.add(tweet_id)

    def build_threads(self) -> List[ConversationThread]:
        """Build all conversation threads."""
        # First, find actual roots (tweets with no valid parent in our dataset)
        all_child_ids = set()
        for children in self._children.values():
            all_child_ids.update(children)

        actual_roots = set()
        for tweet_id, tweet in self._tweets.items():
            if tweet.parent_id is None or tweet.parent_id not in self._tweets:
                actual_roots.add(tweet_id)

        threads = []
        for root_id in actual_roots:
            thread = self._build_thread(root_id)
            if thread:
                threads.append(thread)

        return threads

    def _build_thread(self, root_id: int) -> Optional[ConversationThread]:
        """Build a single conversation thread from a root tweet."""
        if root_id not in self._tweets:
            return None

        tweets = []
        participants = set()
        visited = set()

        def collect(tweet_id: int) -> None:
            if tweet_id in visited or tweet_id not in self._tweets:
                return
            visited.add(tweet_id)

            tweet = self._tweets[tweet_id]
            tweets.append(tweet)
            participants.add(tweet.author_id)

            for child_id in self._children.get(tweet_id, []):
                collect(child_id)

        collect(root_id)

        if not tweets:
            return None

        # Sort by timestamp for chronological order
        tweets.sort(key=lambda t: t.created_at)

        return ConversationThread(
            root_id=root_id,
            tweets=tweets,
            participants=participants,
            start_time=tweets[0].created_at,
            end_time=tweets[-1].created_at,
        )

    def get_thread_for_tweet(self, tweet_id: int) -> Optional[ConversationThread]:
        """Get the thread containing a specific tweet."""
        # Find root by walking up
        current = tweet_id
        while current in self._tweets:
            tweet = self._tweets[current]
            if tweet.parent_id is None or tweet.parent_id not in self._tweets:
                return self._build_thread(current)
            current = tweet.parent_id
        return None

    def clear(self) -> None:
        """Clear all data."""
        self._tweets.clear()
        self._children.clear()
        self._roots.clear()


def merge_threads(
    threads: List[ConversationThread],
) -> Dict[int, ConversationThread]:
    """
    Merge overlapping threads (when a reply connects two previously separate threads).
    Returns mapping of root_id -> thread.
    """
    # Build a union-find structure based on parent relationships
    parent_map: Dict[int, int] = {}

    for thread in threads:
        for tweet in thread.tweets:
            if tweet.parent_id:
                # Union the sets
                root1 = _find_root(parent_map, tweet.id)
                root2 = _find_root(parent_map, tweet.parent_id)
                if root1 != root2:
                    parent_map[root1] = root2

    # Group tweets by their final root
    groups: Dict[int, List[TweetData]] = defaultdict(list)
    all_tweets = [t for thread in threads for t in thread.tweets]

    for tweet in all_tweets:
        root = _find_root(parent_map, tweet.id)
        groups[root].append(tweet)

    # Build merged threads
    merged = {}
    for root_id, tweets in groups.items():
        tweets.sort(key=lambda t: t.created_at)
        participants = {t.author_id for t in tweets}
        merged[root_id] = ConversationThread(
            root_id=root_id,
            tweets=tweets,
            participants=participants,
            start_time=tweets[0].created_at,
            end_time=tweets[-1].created_at,
        )

    return merged


def _find_root(parent_map: Dict[int, int], node: int) -> int:
    """Find root with path compression."""
    if node not in parent_map:
        return node
    root = _find_root(parent_map, parent_map[node])
    parent_map[node] = root  # Path compression
    return root
