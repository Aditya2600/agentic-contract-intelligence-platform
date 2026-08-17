import { queryOptions } from "@tanstack/react-query";
import {
  getCollection,
  getCollections,
  getDocument,
  getDocuments,
  getFindings,
  getRegister,
  getReviewItems,
  getRun,
  getRunEvents,
  getRuns,
} from "./index";

export const collectionsQuery = () =>
  queryOptions({ queryKey: ["collections"], queryFn: getCollections });

export const collectionQuery = (collectionId: string) =>
  queryOptions({
    queryKey: ["collection", collectionId],
    queryFn: () => getCollection(collectionId),
  });

export const registerQuery = (collectionId: string) =>
  queryOptions({
    queryKey: ["register", collectionId],
    queryFn: () => getRegister(collectionId),
  });

export const documentsQuery = (collectionId: string) =>
  queryOptions({
    queryKey: ["documents", collectionId],
    queryFn: () => getDocuments(collectionId),
  });

export const documentQuery = (documentId: string) =>
  queryOptions({
    queryKey: ["document", documentId],
    queryFn: () => getDocument(documentId),
  });

export const runsQuery = (collectionId: string) =>
  queryOptions({ queryKey: ["runs", collectionId], queryFn: () => getRuns(collectionId) });

export const runQuery = (runId: string) =>
  queryOptions({ queryKey: ["run", runId], queryFn: () => getRun(runId) });

export const runEventsQuery = (runId: string) =>
  queryOptions({ queryKey: ["run-events", runId], queryFn: () => getRunEvents(runId) });

export const reviewItemsQuery = (runId: string) =>
  queryOptions({ queryKey: ["review-items", runId], queryFn: () => getReviewItems(runId) });

export const findingsQuery = (runId: string) =>
  queryOptions({ queryKey: ["findings", runId], queryFn: () => getFindings(runId) });
