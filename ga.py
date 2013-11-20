#!/usr/bin/env python
# encoding: utf-8

# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
# 
#     http://www.apache.org/licenses/LICENSE-2.0
# 
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

# author: Paco Nathan
# https://github.com/ceteri/exelixi


from bloomfilter import BloomFilter
from collections import Counter
from hashring import HashRing
from hashlib import sha224
from json import dumps
from random import randint, random, sample


######################################################################
## class definitions

class Population (object):
    def __init__ (self, indiv_instance, prefix="/tmp/exelixi", n_pop=11, term_limit=0.0, hist_granularity=3):
        self.indiv_class = indiv_instance.__class__
        self.prefix = prefix
        self.n_pop = n_pop

        self._term_limit = term_limit
        self._hist_granularity = hist_granularity

        self._uniq_dht = {}
        self._bf = BloomFilter(num_bytes=125, num_probes=14, iterable=[])


    ######################################################################
    ## Individual lifecycle within the local subset of the Population

    def populate (self, current_gen):
        """initialize the population"""
        for _ in xrange(self.n_pop):
            # constructor pattern
            indiv = self.indiv_class()
            indiv.populate(current_gen, indiv.generate_feature_set())

            # add the generated Individual to the Population
            # failure semantics: must filter nulls from initial population
            self.reify(indiv)


    def reify (self, indiv):
        """test/add a newly generated Individual into the Population (birth)"""

        # NB: distribute this operation over the hash ring, through a remote queue
        if not indiv.key in self._bf:
            self._bf.update([indiv.key])

            # NB: potentially the most expensive operation, deferred until remote reification
            indiv.calc_fitness()
            self._uniq_dht[indiv.key] = indiv

            return True
        else:
            return False


    def evict (self, indiv):
        """remove an Individual from the Population (death)"""
        if indiv.key in self._uniq_dht:
            # only need to remove locally
            del self._uniq_dht[indiv.key]

            # NB: serialize to disk (write behinds)
            url = self._get_storage_path(indiv)


    def get_part_hist (self):
        """tally counts for the partial histogram of the fitness distribution"""
        d = dict(Counter([ round(indiv.fitness, self._hist_granularity) for indiv in self._uniq_dht.values() ])).items()
        d.sort(reverse=True)
        return d


    def get_fitness_cutoff (self, selection_rate):
        """determine fitness cutoff (bin lower bounds) for the parent selection filter"""
        sum = 0
        break_next = False

        for bin, count in self.get_part_hist():
            if break_next:
                break

            sum += count
            percentile = sum / float(self.n_pop)
            break_next = percentile >= selection_rate

        return bin


    def _get_storage_path (self, indiv):
        """create a path for durable storage of an Individual"""
        return self.prefix + "/" + indiv.key


    def _boost_diversity (self, current_gen, indiv, mutation_rate):
        """randomly select other individuals and mutate them, to promote genetic diversity"""
        if mutation_rate > random():
            indiv.mutate(self, current_gen)
        else:
            self.evict(indiv)


    def _select_parents (self, current_gen, fitness_cutoff, mutation_rate):
        """select the parents for the next generation"""
        partition = map(lambda x: (x.fitness > fitness_cutoff, x), self._uniq_dht.values())
        good_fit = map(lambda x: x[1], filter(lambda x: x[0], partition))
        poor_fit = map(lambda x: x[1], filter(lambda x: not x[0], partition))

        # randomly select other individuals to promote genetic diversity, while removing the remnant
        for indiv in poor_fit:
            self._boost_diversity(current_gen, indiv, mutation_rate)

        return self._uniq_dht.values()


    def next_generation (self, current_gen, fitness_cutoff, mutation_rate):
        """select/mutate/crossover parents to produce a new generation"""
        parents = self._select_parents(current_gen, fitness_cutoff, mutation_rate)

        for (f, m) in [ sample(parents, 2) for _ in xrange(self.n_pop - len(parents)) ]:
            f.breed(self, current_gen, m)


    def test_termination (self, current_gen):
        """evaluate the terminating condition for this generation and report progress"""
        # find the mean squared error (MSE) of fitness for a population
        hist = self.get_part_hist()
        mse = sum([ count * (1.0 - bin) ** 2.0 for bin, count in hist ]) / float(self.n_pop)

        # report the progress for one generation
        print current_gen, "%.2e" % mse, filter(lambda x: x[1] > 0, hist)

        # stop when a "good enough" solution is found
        return mse <= self._term_limit


    def report_summary (self):
        """report a summary of the evolution"""
        for indiv in sorted(self._uniq_dht.values(), key=lambda x: x.fitness, reverse=True):
            print self._get_storage_path(indiv)
            print "\t".join(["%0.4f" % indiv.fitness, "%d" % indiv.gen, indiv.get_json_feature_set()])


class Individual (object):
    # feature set parameters (customize this part)
    target = 231
    length = 5
    min = 0
    max = 100


    def __init__ (self):
        """create a member of the population"""
        self.gen = None
        self._feature_set = None
        self.key = None
        self.fitness = None


    def populate (self, gen, feature_set):
        """populate the instance variables"""
        self.gen = gen
        self._feature_set = feature_set
        self.key = self.get_unique_key()


    def generate_feature_set (self):
        """generate a new feature set"""
        return sorted([ randint(Individual.min, Individual.max) for _ in xrange(Individual.length) ])


    def get_json_feature_set (self):
        """dump the feature set as a JSON string"""
        return dumps(tuple(self._feature_set))


    def get_unique_key (self):
        """create a unique key by taking a SHA-3 digest of the JSON representing this feature set"""
        m = sha224()
        m.update(self.get_json_feature_set())
        return m.hexdigest()


    def calc_fitness (self):
        """determine the fitness ranging [0.0, 1.0]; higher is better"""
        self.fitness = 1.0 - abs(sum(self._feature_set) - Individual.target) / float(Individual.target)


    def mutate (self, pop, gen):
        """attempt to mutate the feature set"""
        pos_to_mutate = randint(0, len(self._feature_set) - 1)
        mutated_feature_set = self._feature_set
        mutated_feature_set[pos_to_mutate] = randint(Individual.min, Individual.max)

        # constructor pattern
        mutant = self.__class__()
        mutant.populate(gen, sorted(mutated_feature_set))

        # add the mutant Individual to the Population, but remove its prior self
        # failure semantics: ignore, mutation rate is approx upper bounds
        if pop.reify(mutant):
            pop.evict(self)


    def breed (self, pop, gen, mate):
        """breed with a mate to produce a child"""
        half = len(self._feature_set) / 2

        # constructor pattern
        child = self.__class__()
        child.populate(gen, sorted(self._feature_set[half:] + mate._feature_set[:half]))

        # add the child Individual to the Population
        # failure semantics: ignore, the count will rebalance over the hash ring
        pop.reify(child)


if __name__=='__main__':
    pass
