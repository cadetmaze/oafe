"use client";

import {
  CircleNotch,
  Database,
  MagnifyingGlass,
  Tag,
  WarningCircle,
} from "@phosphor-icons/react";
import { useEffect, useRef, useState } from "react";

import WorkspaceExportPanel from "@/components/workspace-export-panel";
import {
  Marker,
  MarkerContent,
  MarkerIcon,
} from "@/components/ui/marker";
import {
  MessageScroller,
  MessageScrollerButton,
  MessageScrollerContent,
  MessageScrollerItem,
  MessageScrollerProvider,
  MessageScrollerViewport,
} from "@/components/ui/message-scroller";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import WorkspacePromptBox from "@/components/workspace-prompt-box";

export type WorkspaceChatEvent = {
  kind: "explored" | "fetching" | "labeling" | "message" | "error";
  role?: "assistant" | "user" | null;
  seq: number;
  text: string;
};

type WorkspaceChatProps = {
  canExport?: boolean;
  events?: readonly WorkspaceChatEvent[];
  exportJobId?: string | null;
  externalAssistantMessage?: {
    id: string;
    markers?: Array<{
      kind: ConversationMarkerKind;
      text: string;
    }>;
    text: string;
  } | null;
  initialQuery: string;
  isThinking?: boolean;
  onSendMessage?: (value: string) => Promise<void> | void;
  requestId: string;
};

type ConversationMarkerKind = Exclude<WorkspaceChatEvent["kind"], "message">;

type TextChatMessage = {
  id: string;
  kind: "message";
  role: "assistant" | "user";
  text: string;
};

type MarkerChatMessage = {
  id: string;
  kind: "marker";
  markerKind: ConversationMarkerKind;
  text: string;
};

type ChatMessage = MarkerChatMessage | TextChatMessage;

const ASSISTANT_DELAY_MS = 700;

function ConversationMarker({
  kind,
  text,
}: {
  kind: ConversationMarkerKind;
  text: string;
}) {
  return (
    <Marker
      className={kind === "error" ? "px-1 text-[#B42318]" : "px-1"}
      role={kind === "error" ? "alert" : "status"}
    >
      <MarkerIcon>
        {kind === "explored" ? (
          <MagnifyingGlass size={13} weight="bold" />
        ) : kind === "fetching" ? (
          <Database size={13} weight="bold" />
        ) : kind === "labeling" ? (
          <Tag size={13} weight="bold" />
        ) : (
          <WarningCircle size={13} weight="bold" />
        )}
      </MarkerIcon>
      <MarkerContent>{text}</MarkerContent>
    </Marker>
  );
}

export default function WorkspaceChat({
  canExport = false,
  events,
  exportJobId = null,
  externalAssistantMessage,
  initialQuery,
  isThinking: controlledIsThinking = false,
  onSendMessage,
  requestId,
}: WorkspaceChatProps) {
  const [messages, setMessages] = useState<ChatMessage[]>(() => [
    {
      id: `${requestId}-initial-user`,
      kind: "message",
      role: "user",
      text: initialQuery,
    },
    {
      id: `${requestId}-initial-explored`,
      kind: "marker",
      markerKind: "explored",
      text: "Explored your request",
    },
    {
      id: `${requestId}-initial-fetching`,
      kind: "marker",
      markerKind: "fetching",
      text: "Fetched 10 candidate examples",
    },
    {
      id: `${requestId}-initial-assistant`,
      kind: "message",
      role: "assistant",
      text: "I found a few examples that seem close. Approve or reject each one so I can refine the search until the results match what you want.",
    },
  ]);
  const [isLocallyThinking, setIsLocallyThinking] = useState(false);
  const [isSending, setIsSending] = useState(false);
  const [submissionErrors, setSubmissionErrors] = useState<MarkerChatMessage[]>([]);
  const messageSequenceRef = useRef(0);
  const lastExternalMessageIdRef = useRef<string | null>(null);
  const replyTimerRef = useRef<number | null>(null);
  const isControlled = events !== undefined;
  const isThinking = isControlled
    ? controlledIsThinking || isSending
    : isLocallyThinking;

  useEffect(() => {
    if (
      isControlled ||
      !externalAssistantMessage ||
      lastExternalMessageIdRef.current === externalAssistantMessage.id
    ) {
      return;
    }

    lastExternalMessageIdRef.current = externalAssistantMessage.id;
    setMessages((currentMessages) => {
      if (
        currentMessages.some(
          (message) => message.id === externalAssistantMessage.id,
        )
      ) {
        return currentMessages;
      }

      const nextMessages = [...currentMessages];

      externalAssistantMessage.markers?.forEach((marker, markerIndex) => {
        nextMessages.push({
          id: `${externalAssistantMessage.id}-marker-${markerIndex}`,
          kind: "marker",
          markerKind: marker.kind,
          text: marker.text,
        });
      });

      nextMessages.push({
        id: externalAssistantMessage.id,
        kind: "message",
        role: "assistant",
        text: externalAssistantMessage.text,
      });

      return nextMessages;
    });
  }, [externalAssistantMessage, isControlled]);

  useEffect(() => {
    return () => {
      if (replyTimerRef.current !== null) {
        window.clearTimeout(replyTimerRef.current);
      }
    };
  }, []);

  async function sendMessage(value: string) {
    if (isThinking) {
      return;
    }

    messageSequenceRef.current += 1;
    const sequence = messageSequenceRef.current;

    if (isControlled) {
      setIsSending(true);

      try {
        if (!onSendMessage) {
          throw new Error("Messaging is not available yet.");
        }

        await onSendMessage(value);
      } catch (error) {
        const detail =
          error instanceof Error && error.message.trim()
            ? ` ${error.message.trim()}`
            : "";

        setSubmissionErrors((currentErrors) => [
          ...currentErrors,
          {
            id: `${requestId}-${sequence}-send-error`,
            kind: "marker",
            markerKind: "error",
            text: `Couldn’t send that message.${detail}`,
          },
        ]);
      } finally {
        setIsSending(false);
      }

      return;
    }

    setMessages((currentMessages) => [
      ...currentMessages,
      {
        id: `${requestId}-${sequence}-user`,
        kind: "message",
        role: "user",
        text: value,
      },
    ]);
    setIsLocallyThinking(true);

    replyTimerRef.current = window.setTimeout(() => {
      setMessages((currentMessages) => [
        ...currentMessages,
        {
          id: `${requestId}-${sequence}-explored`,
          kind: "marker",
          markerKind: "explored",
          text: "Explored your update",
        },
        {
          id: `${requestId}-${sequence}-assistant`,
          kind: "message",
          role: "assistant",
          text: "Got it. I’ll use that to refine the dataset results.",
        },
      ]);
      setIsLocallyThinking(false);
      replyTimerRef.current = null;
    }, ASSISTANT_DELAY_MS);
  }

  const renderedMessages: ChatMessage[] = isControlled
    ? [
        {
          id: `${requestId}-initial-user`,
          kind: "message",
          role: "user",
          text: initialQuery,
        },
        ...events.map<ChatMessage>((event) =>
          event.kind === "message"
            ? {
                id: `${requestId}-event-${event.seq}`,
                kind: "message",
                role: event.role === "user" ? "user" : "assistant",
                text: event.text,
              }
            : {
                id: `${requestId}-event-${event.seq}`,
                kind: "marker",
                markerKind: event.kind,
                text: event.text,
              },
        ),
        ...submissionErrors,
      ]
    : messages;

  return (
    <MessageScrollerProvider autoScroll defaultScrollPosition="end">
      <Tabs
        defaultValue="agent"
        className="flex h-full min-h-0 flex-col bg-white font-geist"
        data-request-id={requestId}
      >
        <div className="shrink-0 px-3 pt-3">
          <TabsList className="h-8 w-full bg-[#F4F4F5] p-1">
            <TabsTrigger
              className="h-6 rounded-md px-2 font-geist text-[12px] font-normal tracking-[-0.01em] text-[#777777] data-active:bg-white data-active:font-medium data-active:text-[#282828]"
              value="agent"
            >
              Agent
            </TabsTrigger>
            <TabsTrigger
              className="h-6 rounded-md px-2 font-geist text-[12px] font-normal tracking-[-0.01em] text-[#777777] data-active:bg-white data-active:font-medium data-active:text-[#282828]"
              value="annotate"
            >
              Annotate
            </TabsTrigger>
            <TabsTrigger
              className="h-6 rounded-md px-2 font-geist text-[12px] font-normal tracking-[-0.01em] text-[#777777] data-active:bg-white data-active:font-medium data-active:text-[#282828]"
              value="export"
            >
              Export Options
            </TabsTrigger>
          </TabsList>
        </div>
        <TabsContent className="flex min-h-0 flex-1 flex-col" value="agent">
          <div className="min-h-0 flex-1">
            <MessageScroller>
              <MessageScrollerViewport aria-label="Conversation">
                <MessageScrollerContent
                  aria-busy={isThinking}
                  className="gap-5 px-4 py-5 text-[14px] leading-5 text-[#282828]"
                >
                  {renderedMessages.map((message) => (
                    <MessageScrollerItem
                      className={
                        message.kind === "marker"
                          ? "-my-1.5 flex justify-start"
                          : message.role === "user"
                            ? "flex justify-end"
                            : "flex justify-start"
                      }
                      key={message.id}
                      messageId={message.id}
                      scrollAnchor={
                        message.kind === "message" && message.role === "user"
                      }
                    >
                      {message.kind === "marker" ? (
                        <ConversationMarker
                          kind={message.markerKind}
                          text={message.text}
                        />
                      ) : message.role === "user" ? (
                        <p className="max-w-[86%] whitespace-pre-wrap rounded-[18px] bg-[#F4F4F5] px-4 py-3">
                          {message.text}
                        </p>
                      ) : (
                        <p className="max-w-[90%] whitespace-pre-wrap px-1">
                          {message.text}
                        </p>
                      )}
                    </MessageScrollerItem>
                  ))}
                  {isThinking && (
                    <MessageScrollerItem
                      className="-my-1.5 flex justify-start"
                      messageId={`${requestId}-thinking`}
                    >
                      <Marker className="px-1" role="status">
                        <MarkerIcon>
                          <CircleNotch
                            className="animate-spin"
                            size={13}
                            weight="bold"
                          />
                        </MarkerIcon>
                        <MarkerContent className="animate-pulse">
                          Thinking…
                        </MarkerContent>
                      </Marker>
                    </MessageScrollerItem>
                  )}
                </MessageScrollerContent>
              </MessageScrollerViewport>
              <MessageScrollerButton />
            </MessageScroller>
          </div>
          <div aria-label="Prompting area" className="h-44 shrink-0 bg-white p-3">
            <WorkspacePromptBox disabled={isThinking} onSubmit={sendMessage} />
          </div>
        </TabsContent>
        <TabsContent className="min-h-0 flex-1" value="annotate">
          <div className="flex h-full items-center justify-center px-6 text-center text-[13px] leading-5 text-[#777777]">
            Annotation tools will appear here when reviewed items are ready.
          </div>
        </TabsContent>
        <TabsContent className="min-h-0 flex-1" keepMounted value="export">
          <WorkspaceExportPanel
            key={requestId}
            enabled={canExport}
            exportJobId={exportJobId}
            requestId={requestId}
          />
        </TabsContent>
      </Tabs>
    </MessageScrollerProvider>
  );
}
