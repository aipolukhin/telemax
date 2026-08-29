"""Known MAX payload keys used by compatibility diagnostics.

This is deliberately a permissive vocabulary, not a validation schema. Missing
fields are normal in an evolving third-party API; an unfamiliar field is only a
signal to log a compatibility warning. Delivery code must continue to handle
the payload defensively instead of rejecting a whole message.
"""

from __future__ import annotations

#: Compatibility vocabulary for payload diagnostics. Keep this a superset:
#: false-positive warnings are worse than silently ignoring an optional field.
APP_WIRE_FIELDS = frozenset({
    "MP4_1080", "_type", "aPlusChannel", "accessType", "accountStatus", "accuracy",
    "actionDestinationType", "admins", "albumName", "alias", "allCanPinMessage", "alt",
    "altitude", "answerId", "answers", "app", "appId", "appState", "appVersion",
    "approxParticipantCount", "artistName", "attach", "audio", "audioGroupIndex", "audioId",
    "audioTrackIndex", "authorType", "availableBySubscription", "backgroundColor",
    "backgroundPlayForbidden", "backwardMarker", "baseIconUrl", "baseRawIconUrl", "baseRawUrl",
    "baseUrl", "bearing", "birthday", "bitrate", "blockedParticipantsCount", "botsInfo",
    "bottom", "button", "buttons", "bytesDownloaded", "call", "callType", "callbackId",
    "channelInfo", "chatFoldersIds", "chatOptions", "chatReactionsSettings", "chatSettings",
    "chatSubject", "chatType", "chunk", "chunks", "cid", "collage", "collapsed", "comments",
    "commentsBlacklistCount", "commentsDisabled", "confirmBeforeSend", "contact", "contactId",
    "contactIds", "contentLevel", "contentLevelChat", "contentType", "contents", "context",
    "control", "conversationId", "convertOptions", "corrupted", "count", "country", "created",
    "crop", "defaultInputDisabled", "delayedChunk", "deleted", "description", "deviceAvatarUrl",
    "deviceId", "deviceName", "dontDisturbUntil", "draft", "draftUpdateTime",
    "draftUpdateTimeForSyncLogic", "duration", "durationLong", "elements", "embedUrl",
    "endTime", "endTrimPosition", "entityId", "entityName", "epu", "event", "expiration",
    "expirationMillis", "expirationTime", "externalSiteName", "favoriteIndex", "file", "fileId",
    "firstMessageId", "firstName", "firstUrl", "flags", "flagsSettings", "forwardMarker", "fps",
    "fragmentsPaths", "framesCount", "frequency", "from", "fullImageUrl", "fullUrl", "gender",
    "gif", "groupChatInfo", "groupId", "groupOptions", "groupPremium", "hangupType", "hasBots",
    "hasNext", "hasPrev", "hdn", "height", "hideLiveLocationPanel",
    "hideLiveLocationPanelBeforeTime", "hideMyLiveLocationPanelBeforeTime", "hidePinnedMessage",
    "host", "icon", "iconHeight", "iconToken", "iconUrl", "iconWidth", "id", "ids",
    "ignoreAutoplay", "image", "imageUrl", "included", "inlineKeyboard", "intent", "invitedBy",
    "inviterId", "isActive", "isAnswered", "isCustomTitle", "isDeleted", "isFull",
    "isImportant", "isMember", "isModerator", "isOriginal", "isProcessingOnServer",
    "isThumbnailInCache", "joinLink", "joinRequest", "joinRequestTime", "joinTime", "keyboard",
    "lastDelayedLoadTime", "lastDelayedUpdateTime", "lastErrorTime", "lastEventTime",
    "lastFireDelayedErrorTime", "lastLocation", "lastMentionMessageId", "lastMessageId",
    "lastModified", "lastName", "lastNotifMark", "lastNotifMessageId", "lastOpenNewMessages",
    "lastOpenPositionOffset", "lastOpenPositionTime", "lastOpenReadMark", "lastPushMessage",
    "lastReactedMessageId", "lastReaction", "lastSearchClickTime",
    "lastShowingUnknownContactBar", "lastStartTimeUpdateTimestamp", "lastSyncTime",
    "lastUpdateTime", "lastWriteTime", "latitude", "left", "length", "link", "linkAttributes",
    "live", "livePeriod", "liveStream", "liveStreamUpdateTime", "localChanges", "localId",
    "localPath", "localPhotoUrl", "location", "longitude", "lottieUrl", "markedAsUnread",
    "marker", "media", "mediaAll", "mediaAudio", "mediaAudioVideoMsg", "mediaCallType",
    "mediaFiles", "mediaId", "mediaLocations", "mediaMusic", "mediaPhotoVideo", "mediaShare",
    "membersCanSeePrivateLink", "membersCount", "menuButton", "message", "messagesTtlSec",
    "messagingPermissions", "metadataId", "modified", "mp4SndUrl", "mp4Url", "mute", "name",
    "names", "newMessages", "nextContentType", "official", "okChat", "onlyAdminCanAddMember",
    "onlyAdminCanCall", "onlyOwnerCanChangeIconTitle", "options", "ordinal", "organizationIds",
    "outgoingMessageId", "owner", "ownerId", "params", "participantSettings",
    "participantsCount", "payload", "pendingJoinRequestsCount", "permissions", "phone", "photo",
    "photoId", "photoToken", "photoUrl", "pinnedMessage", "pinnedMessageId",
    "pinnedMessageServerId", "playRestricted", "poll", "pollId", "present", "presentId",
    "presentJson", "preview", "previewData", "previewParticipantIds", "previewUrl",
    "processingOnServerStatus", "profileOptions", "progress", "progressFloat", "quality",
    "qualityValue", "quickLocation", "rate", "reaction", "reactionIds", "reactions",
    "receiverId", "registrationTime", "replayDelay", "replyButton", "replyKeyboard",
    "replyOrigin", "restrictions", "result", "right", "sections", "sendAction", "senderId",
    "sensitive", "sensitiveContentUnlocked", "sentByPhone", "serverId", "serverPhone",
    "serviceChat", "sessionId", "setId", "settings", "share", "shareId", "shortMessage",
    "showHistory", "showLoading", "signAdmin", "size", "spd", "speed", "startMessage",
    "startPayload", "startTime", "startTrimPosition", "startedAt", "state", "status", "sticker",
    "stickerId", "stickerSets", "stickerType", "stickers", "stickersOrder", "stickersSyncTime",
    "storiesReply", "storyId", "storyOwner", "suspendedBot", "tags", "text", "textColor",
    "thumbhash", "thumbhashData", "thumbnail", "time", "timeout", "timestamp", "title", "token",
    "top", "total", "totalBytes", "totalCount", "track", "transcription", "transcriptionStatus",
    "ttl", "type", "unbindOkPanelCloseTime", "unreadPin", "unreadReply", "updateTime", "url",
    "userId", "userIds", "vcfBody", "version", "video", "videoCollage", "videoConversation",
    "videoId", "videoType", "videoUrl", "voteCount", "voterPreviewIds", "votes", "wave",
    "widget", "width", "yourReaction", "zoom",
})


def keys_the_app_cannot_read(attachment: dict[str, object]) -> list[str]:
    """Keys in this attachment that the MAX app itself would skip. Sorted.

    Empty for everything the protocol has ever been seen to send, which is what
    makes a non-empty answer worth a line in the log.
    """
    return sorted(key for key in attachment if key not in APP_WIRE_FIELDS)
