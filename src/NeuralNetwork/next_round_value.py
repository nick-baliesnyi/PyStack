'''
	Uses the neural net to estimate value at the end of the first betting round.
	Approximates cfvs from current street/round leaf states for first iterations.
	On last iterations, uses next street/round root states and averages them.
'''
import numpy as np

from Settings.arguments import arguments
from Settings.constants import constants
from Game.card_tools import card_tools
from Game.card_to_string_conversion import card_to_string
from Game.card_combinations import card_combinations
from NeuralNetwork.value_nn import ValueNn

class NextRoundValue():
	def __init__(self, street, skip_iterations, leaf_nodes_iterations=0):
		'''
		@param: int :street/round to approximate
		@param: int :iterations to ommit (doesn't matter if approximates leaf/root nodes)
		@param: int :iterations used for faster approximation (approximates current street/round leaf nodes)
		'''
		self.street = street
		# setting up neural network for root nodes of next street and current street leaf nodes
		self.next_street_nn = ValueNn(street+1, approximate='root_nodes', pretrained_weights=True, verbose=0)
		try:
			self.leaf_nodes_nn = ValueNn(street, approximate='leaf_nodes', pretrained_weights=True, verbose=0)
			self.num_leaf_nodes_approximation_iters = leaf_nodes_iterations
		except:
			self.leaf_nodes_nn, self.num_leaf_nodes_approximation_iters = None, 0
			print( "WARNING: leaf node model for street '{}' was not found. using only next street root nodes".format(card_to_string.street_to_name(street)) )


	def _init_root_approximation_vars(self):
		''' same as in self._init_leaf_approximation_vars, just for all possible boards (in next street),
			only difference: it creates cumulative cfvs for every next board '''
		BC, PC, batch_size, HC = self.next_boards_count, constants.players_count, self.batch_size, constants.hand_count
		# init inputs and outputs to neural net
		self.next_round_inputs = np.zeros([batch_size,BC,HC*PC + 1 + self.num_board_features], dtype=arguments.dtype)
		self.next_round_values = np.zeros([batch_size,BC,PC,HC], dtype=arguments.dtype)
		# handling board feature for nn [BC,69] and initing board masks (what hands are possible given that board)
		next_boards_features = np.zeros([BC, self.num_board_features], dtype=arguments.dtype)
		self.next_boards_mask = np.zeros([BC,HC], dtype=bool)
		from tqdm import tqdm
		for i, next_board in enumerate(tqdm(self.next_boards)):
			next_boards_features[i] = card_tools.convert_board_to_nn_feature(next_board)
			self.next_boards_mask[i] = card_tools.get_possible_hands_mask(next_board)
		next_boards_features = np.expand_dims(next_boards_features, axis=0) # reshape: [B,69] -> [1,B,69]
		# repeating next_boards_features: [ 1, B, 69 ] -> [ b, B, 69 ]
		self.next_round_inputs[ : , : , PC*HC+1: ] = np.repeat(next_boards_features, batch_size, axis=0) # [ b, B, PxI +1+69 ] = [ b, B, 69 ]
		# handling pot feature for nn
		# repeating pot_sizes: [b,1] -> [b,B]
		# [ b, B, P x I + 1 + 69 ] = [b,B] / scalar
		self.next_round_inputs[ : , : , PC*HC ] = np.repeat(self.pot_sizes, BC, axis=1) / arguments.stack
		# init normalization (used to normalize values after masking with self.next_boards_mask)
		num_possible_boards = card_combinations.count_next_boards_possible_boards(self.street)
		self.root_nodes_sum_normalization = 1 / num_possible_boards
		# init cumulative cfvs and their normalization (used for self.get_stored_value_on_board())
		self.cumulative_norm = np.zeros([ batch_size, BC, PC ], dtype=arguments.dtype)
		self.cumulative_cfvs = np.zeros([ batch_size, BC, PC, HC ], dtype=arguments.dtype)


	def _init_leaf_approximation_vars(self):
		''' init datastructures, where input is only single board (self.current_board) '''
		PC, batch_size, HC = constants.players_count, self.batch_size, constants.hand_count
		# init inputs and outputs to neural net
		self.current_round_inputs = np.zeros([batch_size, 1,HC*PC + 1 + self.num_board_features], dtype=arguments.dtype)
		self.current_round_values = np.zeros([batch_size, 1,PC,HC], dtype=arguments.dtype)
		# init current board's mask (possible hands, given that board)
		self.current_board_mask = np.zeros([1,HC], dtype=bool)
		self.current_board_mask[0] = card_tools.get_possible_hands_mask(self.current_board)
		# fill inputs with board features
		board_features = card_tools.convert_board_to_nn_feature(self.current_board)
		board_features = np.expand_dims(board_features, axis=0) # reshape: [69] -> [1,69]
		self.current_round_inputs[ : , 0, PC*HC+1: ] = np.repeat(board_features, batch_size, axis=0) # repeat: [1,69] -> [b,69]
		# fill pot sizes factored by stack size
		self.current_round_inputs[ : , : , PC*HC ] = self.pot_sizes / arguments.stack
		# init normalization (used to normalize values after masking with self.current_boards_mask)
		self.leaf_nodes_sum_normalization = 1 / self.current_board_mask.sum()


	def init_computation(self, board, pot_sizes, batch_size):
		'''
		@param: [0-5] :board with 0-5 card int values on it
		@param: [b]   :pot sizes for each state (total states=b)
		@param: int   :batch of how many situations are evaluated simultaneously (usually will be = 1)
		'''
		self.iter = 0
		# setting up current board and possible next boards
		self.current_board = board
		self.next_boards = card_tools.get_next_round_boards(self.current_board)
		self.next_boards_count = self.next_boards.shape[0]
		# init pot sizes [b, 1], where p - number of pot sizes, b - batch size (here not the same as in other files)
		self.pot_sizes = np.repeat(pot_sizes.reshape([-1,1]), batch_size, axis=1)
		self.pot_sizes = self.pot_sizes.reshape([-1,1])
		self.batch_size = self.pot_sizes.shape[0]
		# setting up num board features used in neural network (all boards will give same shape = 69)
		self.num_board_features = card_tools.convert_board_to_nn_feature(np.zeros([])).shape[0]
		# init variables, used for next street root nodes approximation
		# and for current street leaf nodes approximation
		self._init_root_approximation_vars()
		self._init_leaf_approximation_vars()


	def evaluate_ranges(self, ranges):
		''' Gives the predicted counterfactual values at each evaluated state,
			given input ranges. Keeps track of iterations internally, so should
			be called exactly once for every iteration of continual re-solving
		@param: [b,P,I] :ranges, here b is the number of states evaluated (must match input to self.init_computation)
		@return [b,P,I] :cfvs, calculated by averaging all cfvs of next street/round boards
		'''
		PC, HC, batch_size = constants.players_count, constants.hand_count, self.batch_size
		assert(ranges.shape[0] == self.batch_size)
		self.iter += 1
		# check to approximate leafs or next street nodes + avg them
		if self.iter > self.num_leaf_nodes_approximation_iters:
			BC = self.next_boards_count
			neural_network = self.next_street_nn
			nn_inputs = self.next_round_inputs
			nn_outputs = self.next_round_values
			mask = self.next_boards_mask
			sum_normalization = self.root_nodes_sum_normalization
		else:
			BC = 1
			neural_network = self.leaf_nodes_nn
			nn_inputs = self.current_round_inputs
			nn_outputs = self.current_round_values
			mask = self.current_board_mask
			sum_normalization = self.leaf_nodes_sum_normalization
		# copy ranges for all boards (BC)
		ranges = ranges.reshape([batch_size,1,PC,HC]) # [b,P,I] -> [b,1,P,I]
		ranges = np.repeat(ranges, BC, axis=1) # [b,1,P,I] -> [b,B,P,I]
		# mask ranges for not possible hands (given some board (from 2nd axis))
		ranges *= mask.reshape([1,BC,1,HC]) # [b,B,P,I] *= [1,B,1,I]
		# normalizing ranges
		ranges_sum = np.sum(ranges, axis=3) # [b,B,P] = sum([b,B,P,I], axis=2)
		# save var for later on to normalize output values (swaped just like at lookahead.get_results)
		values_norm = np.zeros_like(ranges_sum)
		values_norm[:,:,0] = ranges_sum[:,:,1].copy()
		values_norm[:,:,1] = ranges_sum[:,:,0].copy()
		# eliminating division by 0 and normalizing ranges
		ranges_sum[ ranges_sum == 0 ] = 1
		ranges /= np.expand_dims(ranges_sum, axis=-1) # [b,B,P,I] /= [b,B,P,1]
		# putting ranges into inputs
		nn_inputs[ : , : , :PC*HC ] = ranges.reshape([batch_size,BC,PC*HC])
		del ranges
		# computing value in the next round (outputs are already masked, see neural network)
		neural_network.predict( nn_inputs.reshape([batch_size*BC,-1]), out=nn_outputs.reshape([batch_size*BC,-1]) )
		# normalizing values back to original range sum
		nn_outputs *= values_norm.reshape([batch_size,BC,PC,1]) # [b,B,P,I] *= [b,1,P,1]
		# clip values that are more then maximum
		# 20,000          > nn_value x pot_size > -20,000
		# 20,000/pot_size >       nn_value      > -20,000/pot_size
		max_values = arguments.stack / self.pot_sizes.reshape([batch_size,1,1,1])
		nn_outputs = np.clip(nn_outputs, -max_values, max_values) # [b,B,P,I] = clip([b,B,P,I], [b,1,1,1], [b,1,1,1])
		# calculate normalized sum for each hand and return it
		current_board_values = np.sum(nn_outputs, axis=1) * sum_normalization # [b,P,I] = sum([b,B,P,I], axis=1) * scalar
		# first iterations are ommited and iterations from leaf nodes are ommited too,
		# we only use cfvs generated from root nodes (when transitioning from one street to another)
		if self.iter > arguments.cfr_skip_iters and self.iter > self.num_leaf_nodes_approximation_iters:
			# save values in memory for later (use for self.get_stored_cfvs_of_all_next_round_boards())
			self.cumulative_cfvs += nn_outputs
			self.cumulative_norm += values_norm
		return current_board_values


	def get_stored_cfvs_of_all_next_round_boards(self):
		''' returns stored cfvs for all next boards (computed during resolving) '''
		# remove divison by 0
		self.cumulative_norm[ self.cumulative_norm == 0 ] = 1
		# [b,B,P,I] /= [b,B,P,1] (normalize cfvs)
		self.cumulative_cfvs /= np.expand_dims(self.cumulative_norm, axis=-1)
		return self.cumulative_cfvs





NEXT_ROUND_VALUES = {}
for street in range(1,4):
	street_name = card_to_string.street_to_name(street)
	try:
		NEXT_ROUND_VALUES[street] = NextRoundValue( street, skip_iterations=arguments.cfr_skip_iters,
													leaf_nodes_iterations=arguments.leaf_nodes_iterations[street_name] )
	except:
		print(f"Didn't find neural network for street {street}... In case if this street's nn is not needed, program will not stop.")

def get_next_round_value(street):
	return NEXT_ROUND_VALUES[street]




#
