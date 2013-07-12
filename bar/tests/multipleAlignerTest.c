/*
 * Copyright (C) 2009-2011 by Benedict Paten (benedictpaten@gmail.com)
 *
 * Released under the MIT license, see LICENSE.txt
 */

#include "CuTest.h"
#include "sonLib.h"
#include "multipleAligner.h"
#include "stPosetAlignment.h"
#include "pairwiseAligner.h"
#include "randomSequences.h"

#include <stdlib.h>
#include <string.h>

/*
 * Test the multiple alignment code.
 */

static const char *seq1 = "AGTTT";
static const char *seq2 = "AGTGTG";
static const char *seq3 = "AC";
static const char *seq4 = "";
static stList *littleSeqFrags = NULL;
static PairwiseAlignmentParameters *pabp = NULL;

static void teardown() {
    if (littleSeqFrags != NULL) {
        stList_destruct(littleSeqFrags);
        littleSeqFrags = NULL;
        pairwiseAlignmentBandingParameters_destruct(pabp);
        pabp = NULL;
    }
}

static void setup() {
    teardown();
    littleSeqFrags = stList_construct3(0, (void(*)(void *))seqFrag_destruct);
    pabp = pairwiseAlignmentBandingParameters_construct();
    stList_append(littleSeqFrags, seqFrag_construct(seq1, 0, 0));
    stList_append(littleSeqFrags, seqFrag_construct(seq2, 0, 0));
    stList_append(littleSeqFrags, seqFrag_construct(seq3, 0, 1));
    stList_append(littleSeqFrags, seqFrag_construct(seq4, 1, 0));
}

stSet *makeColumns(stList *seqFrags);

static void test_makeColumns(CuTest *testCase) {
    setup();
    stSet *columns = makeColumns(littleSeqFrags);
    CuAssertIntEquals(testCase, 13, stSet_size(columns));
    stSet_destruct(columns);
    teardown();
}

static void checkAlignment(CuTest *testCase, stList *seqFrags, stList *multipleAlignedPairs) {
    stPosetAlignment *posetAlignment = stPosetAlignment_construct(stList_length(seqFrags));
    //Check the aligned pairs.
    stListIterator *iterator = stList_getIterator(multipleAlignedPairs);
    stIntTuple *multipleAlignedPair;
    while ((multipleAlignedPair = stList_getNext(iterator)) != NULL) {
        CuAssertTrue(testCase, stIntTuple_length(multipleAlignedPair) == 5);
        int64_t score = stIntTuple_get(multipleAlignedPair, 0);
        int64_t seqX = stIntTuple_get(multipleAlignedPair, 1);
        int64_t x = stIntTuple_get(multipleAlignedPair, 2);
        int64_t seqY = stIntTuple_get(multipleAlignedPair, 3);
        int64_t y = stIntTuple_get(multipleAlignedPair, 4);
        st_logInfo("Got aligned pair, score: %" PRIi64 " x seq: %" PRIi64 " x pos: %" PRIi64 " x seq: %" PRIi64 " y pos: %" PRIi64 "\n", score, seqX, x, seqY, y);
        CuAssertTrue(testCase, score > 0);
        CuAssertTrue(testCase, score <= PAIR_ALIGNMENT_PROB_1);
        CuAssertTrue(testCase, seqX >= 0);
        CuAssertTrue(testCase, seqX < stList_length(seqFrags));
        CuAssertTrue(testCase, x >= 0);
        CuAssertTrue(testCase, x < ((SeqFrag *)(stList_get(seqFrags, seqX)))->length);
        CuAssertTrue(testCase, seqY >= 0);
        CuAssertTrue(testCase, seqY < stList_length(seqFrags));
        CuAssertTrue(testCase, y >= 0);
        CuAssertTrue(testCase, y < ((SeqFrag *)(stList_get(seqFrags, seqY)))->length); //strlen(stList_get(sequences, seqY)));
        //Check we can form an alignment
        CuAssertTrue(testCase, stPosetAlignment_add(posetAlignment, seqX, x, seqY, y));
    }
    stList_destructIterator(iterator);
    stPosetAlignment_destruct(posetAlignment);
}

static void test_makeAlignmentUsingAllPairs(CuTest *testCase) {
    setup();
    stList *multipleAlignedPairs = makeAlignmentUsingAllPairs(littleSeqFrags, 0.0, pabp);
    checkAlignment(testCase, littleSeqFrags, multipleAlignedPairs);
    CuAssertIntEquals(testCase, 9, stList_length(multipleAlignedPairs));
    stList_destruct(multipleAlignedPairs);
    teardown();
}

stList *getRandomSeqFrags(int64_t sequenceNumber, int64_t approxLength) {
    /*
     * Generate a random set of sequences.
     */
    stList *seqFrags = stList_construct3(0, (void (*)(void *))seqFrag_destruct);
    char *firstSequence = getRandomSequence(approxLength);
    for (int64_t i = 0; i < sequenceNumber; i++) {
        stList_append(seqFrags, seqFrag_construct(evolveSequence(firstSequence), st_random() > 0.5, st_random() > 0.5));
    }
    return seqFrags;
}

static void test_multipleAlignerAllPairsRandom(CuTest *testCase) {
    for (int64_t test = 0; test < 100; test++) {
        setup();
        stList *randomSeqFrags = getRandomSeqFrags(st_randomInt(0, 10), st_randomInt(0, 100));
        for (int64_t i = 0; i < stList_length(randomSeqFrags); i++) {
            st_logInfo("Sequence to align: %s\n", stList_get(randomSeqFrags, i));
        }
        stList *multipleAlignedPairs = makeAlignmentUsingAllPairs(randomSeqFrags, 0.5, pabp);
        checkAlignment(testCase, randomSeqFrags, multipleAlignedPairs);
        stList_destruct(randomSeqFrags);
        teardown();
    }
}

stList *getReferencePairwiseAlignments(stList *seqs);
static void test_getReferencePairwiseAlignments(CuTest *testCase) {
    setup();
    stList *pairwiseAlignments = getReferencePairwiseAlignments(littleSeqFrags);
    CuAssertIntEquals(testCase, 3, stList_length(pairwiseAlignments));
    CuAssertTrue(testCase, stIntTuple_equalsFn(stIntTuple_construct2( 0, 3), stList_get(pairwiseAlignments, 0)));
    CuAssertTrue(testCase, stIntTuple_equalsFn(stIntTuple_construct2( 0, 2), stList_get(pairwiseAlignments, 1)));
    CuAssertTrue(testCase, stIntTuple_equalsFn(stIntTuple_construct2( 0, 1), stList_get(pairwiseAlignments, 2)));
    stList_destruct(pairwiseAlignments);
    teardown();
}

stList *makeAllPairwiseAlignments(stList *seqs, PairwiseAlignmentParameters *pairwiseAlignmentBandingParameters);
int64_t *getDistanceMatrix(stSet *columns, stList *seqs, int64_t maxPairsToConsider);
stSet *getMultipleSequenceAlignment(stList *seqs, stList *multipleAlignedPairs, double gapGamma, bool checkConsistency);
double subsPerSite(int64_t seq1, int64_t seq2, int64_t *distanceCounts, int64_t seqNo);
static void test_getDistanceMatrix(CuTest *testCase) {
    setup();
    stList *multipleAlignedPairs = makeAllPairwiseAlignments(littleSeqFrags, pabp);
    stSet *columns = getMultipleSequenceAlignment(littleSeqFrags, multipleAlignedPairs, 0.2, 1);
    stSetIterator *setIt = stSet_getIterator(columns);
    Column *c1;
    while ((c1 = stSet_getNext(setIt)) != NULL) {
        do {
            char base1 = ((char *) stList_get(littleSeqFrags, c1->seqName))[c1->position];
            Column *c2 = c1->nColumn;
            while (c2 != NULL) {
                char base2 = ((char *) stList_get(littleSeqFrags, c2->seqName))[c2->position];
                st_logDebug("The pairwise alignment of the little sequences seq1: %" PRIi64 " seq2: %" PRIi64 " pos1: %" PRIi64 " pos2: %" PRIi64 " base1: %c base2: %c \n", c1->seqName, c2->seqName, c1->position, c2->position, base1, base2);
                c2 = c2->nColumn;
            }
            c1 = c1->nColumn;
        } while (c1 != NULL);
    }
    stSet_destructIterator(setIt);
    int64_t *distanceCounts = getDistanceMatrix(columns, littleSeqFrags, 100000);
    CuAssertDblEquals(testCase, 0.2, subsPerSite(0, 1, distanceCounts, 4), 0.00001);
    CuAssertDblEquals(testCase, 0.5, subsPerSite(0, 2, distanceCounts, 4), 0.00001);
    CuAssertDblEquals(testCase, 0.0, subsPerSite(0, 3, distanceCounts, 4), 0.00001);
    CuAssertDblEquals(testCase, 0.5, subsPerSite(1, 2, distanceCounts, 4), 0.00001);
    CuAssertDblEquals(testCase, 0.0, subsPerSite(1, 3, distanceCounts, 4), 0.00001);
    CuAssertDblEquals(testCase, 0.0, subsPerSite(2, 3, distanceCounts, 4), 0.00001);
    for(int64_t seq1=0; seq1<stList_length(littleSeqFrags); seq1++) {
        for(int64_t seq2=seq1+1; seq2<stList_length(littleSeqFrags); seq2++) {
            CuAssertDblEquals(testCase, subsPerSite(seq1, seq2, distanceCounts, 4),  subsPerSite(seq2, seq1, distanceCounts, 4), 0.0);
        }
    }
    stSet_destruct(columns);
    stList_destruct(multipleAlignedPairs);
    teardown();
}

static void test_multipleAlignerRandom(CuTest *testCase) {
    for (int64_t test = 0; test < 100; test++) {
        setup();
        stList *randomSeqFrags = getRandomSeqFrags(st_randomInt(0, 10), st_randomInt(0, 100));
        int64_t spanningTrees = st_randomInt(0, 5);
        for (int64_t i = 0; i < stList_length(randomSeqFrags); i++) {
            st_logInfo("Sequence to align: %s\n", stList_get(randomSeqFrags, i));
        }
        stList *multipleAlignedPairs = makeAlignment(randomSeqFrags, spanningTrees, 10000000, 0.5, pabp);
        checkAlignment(testCase, randomSeqFrags, multipleAlignedPairs);
        stList_destruct(randomSeqFrags);
        teardown();
    }
}

CuSuite* multipleAlignerTestSuite(void) {
    CuSuite* suite = CuSuiteNew();
    SUITE_ADD_TEST(suite, test_getDistanceMatrix);
    SUITE_ADD_TEST(suite, test_getReferencePairwiseAlignments);
    SUITE_ADD_TEST(suite, test_makeColumns);
    SUITE_ADD_TEST(suite, test_makeAlignmentUsingAllPairs);
    SUITE_ADD_TEST(suite, test_multipleAlignerAllPairsRandom);
    SUITE_ADD_TEST(suite, test_multipleAlignerRandom);

    return suite;
}
