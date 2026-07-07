// src/types.ts
export interface SearchInfo {
  stages: any;
  query: any;
  urls: any;
  error: any;
}

export interface Message {
  id: any;
  isUser: boolean;
  searchInfo?: any;
  content?: any;
  isLoading?: any;
  type: any;
}