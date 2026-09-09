import type { DatasetReference } from "@/types/dataset";

export type DatasetContentType = "image" | "video" | "audio" | "document" | "3d";
export type DatasetDuration = "any" | "under-15" | "15-60" | "1-5" | "over-5";
export type DatasetOrientation = "any" | "portrait" | "landscape" | "square";
export type DatasetMatchRange = "very-high" | "high" | "balanced" | "nearby";

export type DatasetRequestOptions = {
  estimatedTimeMinutes: {
    max: number;
    min: number;
  };
  exampleCount: number;
  filters: {
    contentTypes: DatasetContentType[];
    duration: DatasetDuration;
    formats: string[];
    matchRange: DatasetMatchRange;
    orientation: DatasetOrientation;
  };
  seedData: {
    references: DatasetReference[];
    selectedMoodboard: string | null;
    uploads: File[];
  };
};

export type DatasetRequestDraft = DatasetRequestOptions & {
  query: string;
};

export type DatasetRequestPayload = Omit<DatasetRequestDraft, "seedData"> & {
  originContext: {
    category: string | null;
    moodboard: string | null;
  };
  seedData: Omit<DatasetRequestDraft["seedData"], "uploads">;
  uploads: Array<{
    lastModified: number;
    name: string;
    size: number;
    type: string;
  }>;
};
