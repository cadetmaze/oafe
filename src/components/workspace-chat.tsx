"use client";

import { useEffect, useRef, useState } from "react";

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

type WorkspaceChatProps = {
  initialQuery: string;
  requestId: string;
};

type ChatMessage = {
  id: string;
  role: "assistant" | "user";
  text: string;
};

const ASSISTANT_DELAY_MS = 700;

export default function WorkspaceChat({
  initialQuery,
  requestId,
}: WorkspaceChatProps) {
  const [messages, setMessages] = useState<ChatMessage[]>(() => [
    {
      id: `${requestId}-initial-user`,
      role: "user",
      text: initialQuery,
    },
    {
      id: `${requestId}-initial-assistant`,
      role: "assistant",
      text: "I found a few examples that seem close. Approve or reject each one so I can refine the search until the results match what you want.",
    },
  ]);
  const [isThinking, setIsThinking] = useState(false);
  const messageSequenceRef = useRef(0);
  const replyTimerRef = useRef<number | null>(null);

  useEffect(() => {
    return () => {
      if (replyTimerRef.current !== null) {
        window.clearTimeout(replyTimerRef.current);
      }
    };
  }, []);

  function sendMessage(value: string) {
    if (isThinking) {
      return;
    }

    messageSequenceRef.current += 1;
    const sequence = messageSequenceRef.current;

    setMessages((currentMessages) => [
      ...currentMessages,
      {
        id: `${requestId}-${sequence}-user`,
        role: "user",
        text: value,
      },
    ]);
    setIsThinking(true);

    replyTimerRef.current = window.setTimeout(() => {
      setMessages((currentMessages) => [
        ...currentMessages,
        {
          id: `${requestId}-${sequence}-assistant`,
          role: "assistant",
          text: "Got it. I’ll use that to refine the dataset results.",
        },
      ]);
      setIsThinking(false);
      replyTimerRef.current = null;
    }, ASSISTANT_DELAY_MS);
  }

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
                  {messages.map((message) => (
                    <MessageScrollerItem
                      className={message.role === "user" ? "flex justify-end" : "flex justify-start"}
                      key={message.id}
                      messageId={message.id}
                      scrollAnchor={message.role === "user"}
                    >
                      {message.role === "user" ? (
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
                      className="flex justify-start"
                      messageId={`${requestId}-thinking`}
                    >
                      <p className="animate-pulse px-1 text-[#989898]">Thinking…</p>
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
        <TabsContent className="min-h-0 flex-1" value="annotate" />
        <TabsContent className="min-h-0 flex-1" value="export" />
      </Tabs>
    </MessageScrollerProvider>
  );
}
