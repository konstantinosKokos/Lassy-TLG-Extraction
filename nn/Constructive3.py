import torch.nn as nn
import torch
from torch.nn import functional as F
from torch.nn.utils.rnn import *
from utils import SeqUtils
import numpy as np
from collections import defaultdict


def accuracy_new(predictions, truth, phrase_lens):
    """

    :param predictions: mtl, a, sl, bs
    :param truth: mtl, sl, bs
    :return:
    """
    phrase_lens = phrase_lens.to('cpu').numpy().tolist()
    predictions = predictions.argmax(dim=1)
    correct_subtypes = torch.ones(predictions.size()).to('cuda')
    correct_subtypes[predictions.ne(truth)] = 0
    correct_subtypes[truth.eq(0)] = 1
    correct_words = correct_subtypes.prod(dim=1)
    phrases = torch.split(correct_words, split_size_or_sections=phrase_lens)
    correct_phrases = list(map(lambda x: torch.sum(x).item(), phrases))
    correct_phrases = sum(list(map(lambda x: 1 if x[0] == x[1] else 0, zip(correct_phrases, phrase_lens))))
    return (sum(correct_words), correct_words.shape[0]), (correct_phrases, len(phrase_lens))


class Attention(nn.Module):
    def __init__(self, encoder_output_size, device='cuda'):
        super(Attention, self).__init__()
        self.device = device
        self.encoder_output_size = encoder_output_size

        self.key_transformation = nn.Sequential(
            nn.Linear(encoder_output_size, encoder_output_size, bias=False),
            nn.Tanh(),
            nn.Linear(encoder_output_size, encoder_output_size, bias=False),
            nn.Tanh()
        ).to(self.device)
        self.query_transformation = nn.Sequential(
            nn.Linear(encoder_output_size, encoder_output_size, bias=False),
            nn.Tanh(),
            nn.Linear(encoder_output_size, encoder_output_size, bias=False),
            nn.Tanh()
        ).to(self.device)

    def forward(self, sequence):
        sequence = sequence.transpose(1, 0)
        # sequence : batch_size, seq_len, encoder_size ~~ 256, 30, 300
        keys = self.key_transformation(sequence)  # batch_size, seq_len, key_size  ~~ 256, 30, 300
        queries = self.query_transformation(sequence).transpose(2, 1)  # batch_size, key_size, seq_len  ~ 256, 300, 30

        # weights[b,i,k] = Σ keys[b,i,j] * queries[b,j,k]  -- inner product across the sizes
        # weights = torch.einsum('bij,bjk->bik', [keys, queries])  # batch_size, seq_len, seq_len ~ 256, 30, 30
        weights = torch.bmm(keys, queries) / \
                    torch.sqrt(torch.tensor(self.encoder_output_size).float()).to(self.device)

        # now weights[s,k,q] tells us the scoring of query q against key k in sentence s
        # we need to normalize so that Σ weights[b,i,:] = 1 (the sum of all query-scores against the same key is 1)
        weights = F.softmax(weights, dim=-1)  # batch_size, seq_len, seq_len

        # attention is given as attention[b,i,l] = Σ weights[b,i,j] * vectors[b, j, l]
        # attended = torch.einsum('bij,bjl->bil', [weights, sequence])
        attended = torch.bmm(weights, sequence)  # ~ 256, 30, 300
        return attended.transpose(1, 0)


class Decoder(nn.Module):
    def __init__(self, encoder_output_size, num_atomic, hidden_size, device, sos, max_steps=40, embedding_size=50):
        super(Decoder, self).__init__()
        self.sos = sos
        self.device = device
        self.max_steps = max_steps
        self.num_atomic = num_atomic
        self.hidden_size = hidden_size
        self.encoder_output_size = encoder_output_size
        self.embedding_size = embedding_size

        self.body = nn.GRU(input_size=embedding_size, hidden_size=hidden_size, num_layers=2, dropout=0.5).to(device)
        self.embedder = nn.Embedding(num_embeddings=self.num_atomic, embedding_dim=self.embedding_size, padding_idx=0)
        self.hidden_to_output = nn.Sequential(
            nn.Linear(in_features=hidden_size, out_features=num_atomic)).to(device)
        self.encoder_to_h0 = nn.Sequential(
            nn.Linear(in_features=encoder_output_size, out_features=self.body.num_layers*hidden_size),
            nn.Tanh()
        ).to(device)

    def forward(self, encoder_output, batch_y=None):
        # training -- fast mode
        if batch_y is not None:
            unsorted_batch_y, sequence_lengths = pad_packed_sequence(batch_y)
            unsorted_embeddings = self.embedder(unsorted_batch_y)
            unsorted_embeddings = pack_padded_sequence(unsorted_embeddings, sequence_lengths)
            indices = reindex(unsorted_embeddings.batch_sizes)
            sorted_embeddings = torch.Tensor.index_select(unsorted_embeddings.data, 0, indices).permute(1, 0, 2)

            h_0 = self.encoder_to_h0(encoder_output).reshape(-1, 2, self.hidden_size).permute(1, 0, 2).contiguous()
            # c_0 = self.encoder_to_c0(encoder_output).reshape(-1, 2, self.hidden_size).permute(1, 0, 2).contiguous()

            h_t, _ = self.body.forward(sorted_embeddings, h_0)  # mts, msl*bs, h

            y_t = self.hidden_to_output(h_t)
            y_t = F.log_softmax(y_t, dim=-1)
            y_t = y_t[:-1, :, :]  # mtl-1, nw, atomic
            return y_t

        # validation -- slow mode
        h_t = self.encoder_to_h0(encoder_output).reshape(-1, 2, self.hidden_size).permute(1, 0, 2).contiguous()
        # c_t = self.encoder_to_c0(encoder_output).reshape(-1, 2, self.hidden_size).permute(1, 0, 2).contiguous()
        sos = (torch.ones(1, h_t.shape[1]) * self.sos).to(self.device).long()
        e_t = self.embedder(sos)

        Y = []
        for t in range(self.max_steps):
            _, h_t = self.body.forward(e_t, h_t)
            y_t = self.hidden_to_output(h_t[1])
            y_t = F.log_softmax(y_t, dim=-1)
            Y.append(y_t)
            p_t = y_t.argmax(dim=-1)
            e_t = self.embedder(p_t).unsqueeze(0)
        return torch.stack(Y)[:-1]


class Model(nn.Module):
    def __init__(self, num_atomic, max_steps, device, sos=None, num_types=None):
        super(Model, self).__init__()
        self.device = device
        self.num_atomic = num_atomic
        self.num_types = num_types
        self.mode = None

        self.word_encoder = nn.LSTM(input_size=300, hidden_size=300,
                                    bidirectional=True, num_layers=2, dropout=0.5).to(device)
        self.attention = Attention(device=device, encoder_output_size=300)
        self.type_decoder = Decoder(encoder_output_size=300, num_atomic=num_atomic,
                                    hidden_size=384, device=self.device, max_steps=max_steps,
                                    sos=sos).to(device)

    def forward_core(self, word_vectors):
        encoder_o, _ = self.word_encoder(word_vectors)  # num_words, 2 * h
        encoder_o, seq_lens = pad_packed_sequence(encoder_o)
        encoder_o = encoder_o[:, :, :self.word_encoder.hidden_size] + encoder_o[:, :, self.word_encoder.hidden_size:]
        encoder_o = self.attention(encoder_o)
        encoder_o = pack_padded_sequence(encoder_o, seq_lens)
        indices = reindex(encoder_o.batch_sizes)
        ordered_encoder_output = torch.index_select(encoder_o.data, 0, indices)
        return ordered_encoder_output

    def forward_constructive(self, encoder_o, batch_y):
        construction = self.type_decoder(encoder_o, batch_y)
        return construction

    def forward(self, word_vectors, batch_y=None):
        encoder_output = self.forward_core(word_vectors)
        return self.forward_constructive(encoder_output, batch_y)

    def iter_epoch(self, dataset, batch_size, criterion, optimizer, iter_indices=None, mode='train'):
        if iter_indices is None:
            permutation = np.random.permutation(len(dataset))
        else:
            permutation = np.random.permutation(iter_indices)

        loss = 0.
        batch_start = 0

        correct_words, total_words, correct_phrases, total_phrases = 0, 0, 0, 0

        while batch_start < len(permutation):
            batch_end = min([batch_start + batch_size, len(permutation)])

            # perform bucketing on the batch (-> mini-batching)
            batch_all = sorted([dataset[iter_indices[i]] for i in range(batch_start, batch_end)],
                               key=lambda x: x[0].shape[0], reverse=True)

            batch_x = pack_sequence([x[0] for x in batch_all]).to(self.device)
            batch_y = pack_sequence([torch.stack(x[2]) for x in batch_all]).to(self.device)

            if mode == 'train':
                batch_loss, (batch_correct_words, batch_total_words), (batch_correct_phrases, batch_total_phrases) = \
                    self.train_batch(batch_x, batch_y, criterion, optimizer)
            elif mode == 'eval':
                batch_loss, (batch_correct_words, batch_total_words), (batch_correct_phrases, batch_total_phrases) = \
                    self.eval_batch(batch_x, batch_y, criterion)
            else:
                raise ValueError('Unknown mode.')

            loss += batch_loss
            correct_words += batch_correct_words
            total_words += batch_total_words
            correct_phrases += batch_correct_phrases
            total_phrases += batch_total_phrases

            batch_start += batch_size
        return loss, correct_words/total_words, correct_phrases/total_phrases

    # def truncated_train_batch(self, batch_x, batch_y, criterion, optimizer):
    #     self.train()
    #     optimizer.zero_grad()
    #
    #     loss = 0.
    #     encoder_output = self.forward_core(batch_x)
    #     partial_y, h_n, partial_embeddings, msl, bs = \
    #         self.type_decoder.truncated_forward_first_step(encoder_output, batch_y)
    #     partial_loss =

    def train_batch(self, batch_x, batch_y, criterion, optimizer):
        self.train()
        optimizer.zero_grad()

        prediction = self.forward(batch_x, batch_y).permute(1, 2, 0)  # NW, A, TS

        indices = reindex(batch_y.batch_sizes)
        _, phrase_lens = pad_packed_sequence(batch_y)
        batch_y = batch_y.data[indices][:, 1:]  # NW, TS

        loss = criterion(prediction, batch_y)
        weight = (1 + torch.arange(loss.shape[1]).float().to(self.device) / 100).repeat(loss.shape[0], 1)
        loss = loss * weight
        loss = loss[loss != 0.].sum() / batch_y.shape[0]
        loss.backward()

        (batch_correct, batch_total), (sentence_correct, sentence_total) = accuracy_new(prediction,
                                                                                        batch_y, phrase_lens)
        optimizer.step()
        return loss.item(), (batch_correct, batch_total), (sentence_correct, sentence_total)

    def eval_batch(self, batch_x, batch_y, criterion):
        self.eval()

        prediction = self.forward(batch_x).permute(1, 2, 0)

        indices = reindex(batch_y.batch_sizes)
        _, phrase_lens = pad_packed_sequence(batch_y)
        batch_y = torch.index_select(batch_y.data, 0, indices)[:, 1:]

        loss = criterion(prediction, batch_y)
        loss = loss[loss != 0.].sum() / batch_y.shape[0]

        (batch_correct, batch_total), (sentence_correct, sentence_total) = accuracy_new(prediction,
                                                                                        batch_y, phrase_lens)

        return loss.item(), (batch_correct, batch_total), (sentence_correct, sentence_total)


def __main__(fake=False, mini=False, language='nl'):
    if language == 'nl':
        s = SeqUtils.__main__(fake=fake, constructive=True, sequence_file='test-output/sequences/words-types.p',
                              return_types=True, mini=mini, language='nl')
    elif language == 'fr':
        s = SeqUtils.__main__(fake=fake, constructive=True, sequence_file='test-output/sequences/words-types_fr.p',
                              return_types=True, mini=mini, language='fr', max_sentence_length=35,)
    print(s.atomic_dict)

    if fake:
        print('Warning! You are using fake data!')

    num_epochs = 1000
    batch_size = 128
    val_split = 0.25

    indices = [i for i in range(len(s))]
    splitpoint = int(np.floor(val_split * len(s)))
    np.random.shuffle(indices)
    train_indices, val_indices = indices[splitpoint:], indices[:splitpoint]
    val_indices = sorted(val_indices, key=lambda i: s[i][0].shape[0], reverse=True)  # sort val_indices for bucketing

    marks = SeqUtils.get_type_occurrences([s.type_sequences[i] for i in train_indices])

    print('Training on {} and validating on {} samples.'.format(len(train_indices), len(val_indices)))

    curriculum = organize_curriculum(s, train_indices, marks)
    difficulty = 0

    device = ('cuda' if torch.cuda.is_available() else 'cpu')
    print('Using {}'.format(device))

    ecdc = Model(num_atomic=len(s.atomic_dict), device=device, max_steps=s.max_type_len, num_types=len(s.types),
                 sos=s.inverse_atomic_dict['<SOS>'],)
    criterion = nn.NLLLoss(reduction='none', ignore_index=s.inverse_atomic_dict['<PAD>'])
    optimizer = torch.optim.RMSprop(ecdc.parameters(), lr=1e-03, weight_decay=3e-04)
    scheduler = torch.optim.lr_scheduler.ReduceLROnPlateau(optimizer, patience=7, verbose=True, threshold=0.001,
                                                           factor=0.5, threshold_mode='rel', cooldown=0, min_lr=1e-09,
                                                           eps=1e-08)

    val_history = []

    store_samples(ecdc, s, val_indices, marks=marks)

    for i in range(num_epochs):
        print('================== Epoch {} =================='.format(i))
        l, a, b = ecdc.iter_epoch(s, batch_size, criterion, optimizer, curriculum[difficulty])
        if a > 0.85 and difficulty != len(curriculum) - 1:
            difficulty += 1
            print('Raising difficulty: {}'.format(difficulty))
            scheduler = torch.optim.lr_scheduler.ReduceLROnPlateau(optimizer, patience=7, verbose=True,
                                                                   threshold=0.001,
                                                                   factor=0.5, threshold_mode='rel', cooldown=0,
                                                                   min_lr=1e-09,
                                                                   eps=1e-08)
        else:
            scheduler.step(l)

        print(' Training Loss: {}'.format(l))
        print(' Training Word Accuracy: {}'.format(a))
        print(' Training Phrase Accuracy : {}'.format(b))

        if i % 5 == 0 and i != 0:
            l, a, b = ecdc.iter_epoch(s, 256, criterion, optimizer, val_indices, mode='eval')
            print('- - - - - - - - - - - - - - - - - - - - - - - - - ')
            print(' Validation Loss: {}'.format(l))
            print(' Validation Word Accuracy: {}'.format(a))
            print(' Validation Phrase Accuracy : {}'.format(b))
            val_history.append(l)
            if min(val_history) == l:
                print('- - - - - - - - - - - - - - - - - - - - - - - - - ')
                print(' & & & & & & & & & & & & & & & & & & & & & & & & & & '
                      'Best validation score at epoch {}. Storing results..'
                      ' & & & & & & & & & & & & & & & & & & & & & & & & & &'.format(i))
                store_samples(ecdc, s, val_indices, marks=marks)


def store_samples(network, dataset, val_indices, device='cuda', batch_size=128, log_file='nn/val_log.tsv', marks=None):

    texts = []
    t_types = []
    p_types = []

    batch_start = 0

    while batch_start < len(val_indices):

        batch_end = min([batch_start + batch_size, len(val_indices)])
        batch_indices = [val_indices[i] for i in range(batch_start, batch_end)]
        sorted_indices = batch_indices
        batch_all = [dataset[i] for i in sorted_indices]

        batch_x = pack_sequence([x[0] for x in batch_all]).to(device)
        batch_y = pack_sequence([torch.stack(x[2]) for x in batch_all]).to(device)
        _, phrase_lens = pad_packed_sequence(batch_y)
        phrase_lens = phrase_lens.cpu().numpy().tolist()
        indices = reindex(batch_y.batch_sizes)
        batch_y = torch.index_select(batch_y.data, 0, indices)[:, 1:]
        batch_y = torch.split(batch_y, phrase_lens)
        batch_y = list(map(lambda x: x.cpu().numpy().tolist(), batch_y))

        prediction = network.forward(batch_x).argmax(dim=-1).permute(1, 0)
        prediction = torch.split(prediction, phrase_lens)
        prediction = list(map(lambda x: x.cpu().numpy().tolist(), prediction))

        texts.extend([dataset.word_sequences[i] for i in sorted_indices])

        batch_t_types = SeqUtils.convert_many_vector_sequences_to_type_sequences(batch_y, dataset.atomic_dict)
        batch_t_types = [[t for t in batch_t_types[i] if t] for i in range(len(batch_t_types))]
        t_types.extend(batch_t_types)

        batch_p_types = SeqUtils.convert_many_vector_sequences_to_type_sequences(prediction, dataset.atomic_dict)
        p_types.extend([[batch_p_types[i][j] for j in range(len(batch_t_types[i]))] for i in range(len(batch_t_types))])

        batch_start = batch_end

    imagined_types = SeqUtils.get_all_unique(p_types) - SeqUtils.get_all_unique(t_types)

    with open(log_file, 'w') as f:
        for i in range(len(texts)):
            for j in range(len(texts[i])):
                f.write(texts[i][j].replace('\t', ' ').replace('\n', ' ') +
                        '\t' + str(marks[t_types[i][j]]) +
                        '\t' + str(int(t_types[i][j] == p_types[i][j])) +
                        '\t' + t_types[i][j] +
                        '\t' + p_types[i][j] + '\n')
            f.write('\n')


def reindex(batch_sizes):
    current = 0
    indices = []
    while current < batch_sizes[0]:
        for i in range(len(batch_sizes[batch_sizes > current])):
            index = torch.tensor(current) + sum(batch_sizes[:i])
            indices.append(index)
        current += 1
    return torch.Tensor(indices).to('cuda').long()


def organize_curriculum(dataset, training_indices, marks):
    curriculum = []
    # the number of words in the sample
    s_lens = list(map(len, [dataset.word_sequences[i] for i in training_indices]))
    # the maximum type length in the sample
    t_lens = list(map(lambda x: max(list(map(len, x))), [dataset.type_vectors[i] for i in training_indices]))
    # the minimum of type occurrences in the sample
    rarities = list(map(lambda x: min(list(map(lambda y: marks[y], x))),
                        [dataset.type_sequences[i] for i in training_indices]))

    curriculum.append([index for i, index in enumerate(training_indices)
                       if s_lens[i] < 10 and t_lens[i] < 6 and rarities[i] > 2000])
    curriculum.append([index for i, index in enumerate(training_indices)
                       if s_lens[i] < 20 and t_lens[i] < 10 and rarities[i] > 2000])
    curriculum.append([index for i, index in enumerate(training_indices)
                       if s_lens[i] <= 25 and t_lens[i] < 14 and rarities[i] > 1500])
    curriculum.append([index for i, index in enumerate(training_indices)
                       if s_lens[i] <= 25 and t_lens[i] < 18 and rarities[i] > 1000])
    curriculum.append([index for i, index in enumerate(training_indices)
                       if s_lens[i] <= 30 and t_lens[i] < 22 and rarities[i] > 800])
    curriculum.append([index for i, index in enumerate(training_indices)
                       if s_lens[i] <= 30 and t_lens[i] < 26 and rarities[i] > 500])
    curriculum.append([index for i, index in enumerate(training_indices)
                       if t_lens[i] < 30 and rarities[i] > 500])
    curriculum.append([index for i, index in enumerate(training_indices)
                       if t_lens[i] < 34 and rarities[i] > 100] +
                      [index for i, index in enumerate(training_indices)
                       if t_lens[i] < 38 and 100 < rarities[i] < 500] * 2)
    curriculum.append([index for i, index in enumerate(training_indices)
                       if rarities[i] > 100] +
                      [index for i, index in enumerate(training_indices)
                       if t_lens[i] < 38 and 100 < rarities[i] < 500] * 2)
    # unbound and oversample the rarity
    curriculum.append(training_indices +
                      [index for i, index in enumerate(training_indices)
                       if rarities[i] < 500] * 2 +
                      [index for i, index in enumerate(training_indices)
                       if rarities[i] < 100] * 2 +
                      [index for i, index in enumerate(training_indices)
                       if rarities[i] < 20] * 2
                      )
    print('Curriculum lens: {}'.format(list(map(len, curriculum))))
    return curriculum
